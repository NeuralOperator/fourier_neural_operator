import pytest
import torch
from ..discrete_continuous_convolution import (DiscreteContinuousConv2d, 
                                               DiscreteContinuousConvTranspose2d, 
                                               EquidistantDiscreteContinuousConv2d,
                                               EquidistantDiscreteContinuousConvTranspose2d)

from ..embeddings import regular_grid_2d

# global params
batch_size = 4
in_channels = 3
out_channels = 1
side_length_in = 64
side_length_out = 48

device = "cuda" if torch.backends.cuda.is_built() else "cpu"

@pytest.mark.parametrize('conv_type', [DiscreteContinuousConv2d,
                                       DiscreteContinuousConvTranspose2d])
def test_regular_disco_conv2d(conv_type):
    # create regular grids of in and output coords
    grid_in = torch.stack(regular_grid_2d(spatial_dims=[side_length_in, side_length_in]))
    grid_out = torch.stack(regular_grid_2d(spatial_dims=[side_length_out, side_length_out]))
    
    # reshape grids to point clouds (channels, n_pts)
    grid_in = grid_in.view(2, -1)
    grid_out = grid_out.view(2, -1)

    # quad weights: one weight per point
    quad_weights = torch.randn(grid_in.shape[1])

    conv_layer = conv_type(
        in_channels=in_channels,
        out_channels=out_channels,
        grid_in=grid_in,
        grid_out=grid_out,
        kernel_shape=3,
        quad_weights=quad_weights
    )

    # start with a grid, pass to forward as a point cloud
    x = torch.randn(batch_size, in_channels, side_length_in, side_length_in)
    x = x.reshape(batch_size, in_channels, -1)

    res = conv_layer(x)
    assert res.shape == (batch_size, out_channels, side_length_out ** 2)

@pytest.mark.parametrize('conv_type', [EquidistantDiscreteContinuousConv2d,
                                       EquidistantDiscreteContinuousConvTranspose2d])
def test_equidistant_disco_conv2d(conv_type):

    in_shape = (side_length_in, side_length_in)
    if conv_type == EquidistantDiscreteContinuousConv2d:
        out_shape = (side_length_in // 2, side_length_in // 2)
    else:
        out_shape = (side_length_in * 2, side_length_in * 2)
    
    conv_layer = conv_type(
        in_channels=in_channels,
        out_channels=out_channels,
        in_shape=in_shape,
        out_shape=out_shape,
        kernel_shape=3,
    )

    # start with a grid, pass to forward as a grid
    x = torch.randn(batch_size, in_channels, *in_shape)

    res = conv_layer(x)
    assert res.shape == (batch_size, out_channels, *out_shape)