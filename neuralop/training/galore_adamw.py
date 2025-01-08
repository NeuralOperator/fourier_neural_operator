import math
import warnings
from typing import Callable, Iterable, Tuple, Union, List

import torch
from torch import nn

from .adamw import AdamW
from .tensor_galore_projector import TensorGaLoreProjector


class GaLoreAdamW(AdamW):
    """
    Implements AdamW with Tensor-GaLore projection (see [1]_ and [2]_).

    Parameters
    ----------
    params: `Iterable[nn.parameter.Parameter]`:
        Iterable of parameters to optimize or dictionaries defining parameter groups.
    galore_params: `Iterable[nn.parameter.Parameter]`:
        Iterable of parameters to optimize in low-rank subspace
    galore_rank : float, int or int tuple
        Rank of low-rank subspace in which to optimize `galore_params`.
        Either a float corresponding to a percentage of params
        to preserve, or an list of int ranks corresponding to
        each mode of the tensor. If a single int is given, it is used 
        for all modes (see `neuralop/training/tensor_galore_projector.py`)
    lr : `float`, *optional*, defaults to 0.001:
        The learning rate to use.
    betas : `Tuple[float,float]`, *optional*, defaults to `(0.9, 0.999)`
        Adam's betas parameters (b1, b2).
    eps : `float`, *optional*, defaults to 1e-06:
        Adam's epsilon for numerical stability.
    weight_decay : `float`, *optional*, defaults to 0.0:
        Decoupled weight decay to apply.
    correct_bias : `bool`, *optional*, defaults to `True`:
        Whether or not to correct bias in Adam (for instance, in Bert TF repository they use `False`).
    activation_checkpoint: bool, default True
        whether to use activation checkpointing during projection
    no_deprecation_warning : `bool`, *optional*, defaults to `False`:
        A flag used to disable the deprecation warning (set to `True` to disable the warning).

    References
    ----------
    .. _[1] : Zhao, J. et al. (2024). GaLore: Memory-Efficient LLM Training
        by Gradient Low-Rank Projection. ICML 2024, https://arxiv.org/abs/2403.03507.
    
    .. _[2] : George, R.J. et al. (2024). Tensor-GaLore: Memory-Efficient
        Training via Gradient Tensor Decomposition. NeurIPS 2024 OPT workshop, 
        https://openreview.net/pdf?id=sBaUZzZXJN.
    """

    def __init__(
        self,
        params: Iterable[nn.parameter.Parameter],
        galore_params: Iterable[nn.parameter.Parameter],
        galore_rank: Union[float, int, Tuple[int]], 
        lr: float = 1e-3,
        betas: Tuple[float, float] = (0.9, 0.999),
        eps: float = 1e-6,
        weight_decay: float = 0.0,
        correct_bias: bool = True,
        activation_checkpoint: bool = True,
        no_deprecation_warning: bool = False,
        warm_restart: bool=True,
    ):
        
        super().__init__(params, lr, betas, eps, weight_decay, correct_bias, no_deprecation_warning,)
        # Keep GaLore parameters separate for projection
        self.add_param_group({'params': galore_params,
                              'rank': galore_rank,
                              'lr': lr,
                              'betas': betas,
                              'eps': eps,
                              'weight_decay': weight_decay,
                              'correct_bias': correct_bias,
                              'galore': True
                              })
        self.activation_checkpoint = activation_checkpoint
        self.warm_restart = warm_restart

    @torch.no_grad()
    def step(self, closure: Callable = None):
        """
        Performs a single optimization step.

        Arguments:
            closure (`Callable`, *optional*): A closure that reevaluates the model and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                grad = p.grad
                if grad.is_sparse:
                    raise RuntimeError("Adam does not support sparse gradients, please consider SparseAdam instead")

                state = self.state[p]
                
                if "step" not in state:
                    state["step"] = 0
                
                # GaLore Projection
                if group.get('galore', False):
                    if "projector" not in state:
                        state["projector"] = TensorGaLoreProjector(
                            group["rank"], 
                            update_proj_gap=group["update_proj_gap"],
                            scale=group["scale"], 
                            proj_type=group["proj_type"], 
                            activation_checkpoint=self.activation_checkpoint,
                            warm_restart=self.warm_restart)
                    
                    # track tensor shape for projection back
                    proj_input = grad
                    grad = state["projector"].project(proj_input, state["step"])

                # State initialization
                if "exp_avg" not in state:
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(grad)
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(grad)

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
                beta1, beta2 = group["betas"]

                state["step"] += 1

                # Decay the first and second moment running average coefficient
                # In-place operations to update the averages at the same time
                exp_avg.mul_(beta1).add_(grad, alpha=(1.0 - beta1))
                if torch.is_complex(grad) and self.support_complex:
                    exp_avg_sq.mul_(beta2).addcmul_(grad, grad.conj(), value=1.0 - beta2)
                else:
                    exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1.0 - beta2)
                denom = exp_avg_sq.sqrt().add_(group["eps"])

                step_size = group["lr"]
                if group["correct_bias"]:  # No bias correction for Bert
                    bias_correction1 = 1.0 - beta1 ** state["step"]
                    bias_correction2 = 1.0 - beta2 ** state["step"]
                    step_size = step_size * math.sqrt(bias_correction2) / bias_correction1

                # compute norm gradient
                norm_grad = exp_avg / denom
                
                # GaLore Projection Back
                if group.get('galore', False):
                    norm_grad = state["projector"].project_back(norm_grad)
                    
                p.add_(norm_grad, alpha=-step_size)

                # Just adding the square of the weights to the loss function is *not*
                # the correct way of using L2 regularization/weight decay with Adam,
                # since that will interact with the m and v parameters in strange ways.
                #
                # Instead we want to decay the weights in a manner that doesn't interact
                # with the m/v parameters. This is equivalent to adding the square
                # of the weights to the loss with plain (non-momentum) SGD.
                # Add weight decay at the end (fixed version)
                if group["weight_decay"] > 0.0:
                    p.add_(p, alpha=(-group["lr"] * group["weight_decay"]))

        return loss