# -------------------------------------------#
#             Backbone modules.              #
# -------------------------------------------#

import torch
import torch.nn.functional as F
import torchvision
from torch import nn
from torchvision.models._utils import IntermediateLayerGetter
from typing import Dict, List
from collections import OrderedDict
from util.misc import NestedTensor, is_main_process
from .position_encoding import build_position_encoding
from .swin_transformer import build_swin_transformer


class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt,
    without which any other models than torchvision.models.resnet[18,34,50,101]
    produce nans.
    """

    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()  # w * 1 / sqrt{var + \epsilon}
        bias = b - rm * scale  # bias - (mean * w) / sqrt{var + \epsilon}
        return x * scale + bias  # (x * w) / sqrt{var + \epsilon} + ...


class BackboneBase(nn.Module):
    def __init__(self, backbone: nn.Module, train_backbone: bool, num_channels: int, return_interm_layers: bool):
        super().__init__()
        for name, parameter in backbone.named_parameters():
            if not train_backbone or 'layer2' not in name and 'layer3' not in name and 'layer4' not in name:
                parameter.requires_grad_(False)
        if return_interm_layers:
            return_layers = {"layer1": "0", "layer2": "1", "layer3": "2", "layer4": "3"}
        else:
            return_layers = {'layer4': "0"}

        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers)
        self.num_channels = num_channels

    def forward(self, tensor_list: NestedTensor):
        '''
        :param tensor_list: NestedTensor (tensor + mask)
        :return:
        '''
        xs = self.body(tensor_list.tensors)
        out: Dict[str, NestedTensor] = {}
        for name, x in xs.items():
            m = tensor_list.mask
            assert m is not None

            # mask 是上采样的特征
            mask = F.interpolate(m[None].float(), size=x.shape[-2:]).to(torch.bool)[0]
            out[name] = NestedTensor(x, mask)
        return out


class Backbone(BackboneBase):
    """
    ResNet backbone with frozen BatchNorm.
    """

    def __init__(self, name: str,
                 train_backbone: bool,
                 return_interm_layers: bool,
                 dilation: bool,
                 batch_norm=FrozenBatchNorm2d):
        if name in ['resnet18', 'resnet34', 'resnet50', 'resnet101']:
            backbone = getattr(torchvision.models, name)(
                replace_stride_with_dilation=[False, False, dilation],
                pretrained=is_main_process(), norm_layer=batch_norm)

        # 用Swin-T作为backbone
        elif name in ['swin_B_224_22k', 'swin_B_384_22k', 'swin_L_224_22k', 'swin_L_384_22k']:
            imgsize = int(name.split('_')[-2])
            backbone = build_swin_transformer(name, imgsize)

        num_channels = 512 if name in ('resnet18', 'resnet34') else 2048
        super().__init__(backbone, train_backbone, num_channels, return_interm_layers)


class Joiner(nn.Sequential):
    def __init__(self, backbone, position_embedding):
        super().__init__(backbone, position_embedding)

    def forward(self, tensor_list: NestedTensor):
        # nn.Sequential[0](tensor_list)
        xs = self[0](tensor_list)
        out: List[NestedTensor] = []
        pos = []
        for name, x in xs.items():
            out.append(x)
            # position encoding
            pos.append(self[1](x).to(x.tensors.dtype))

        return out, pos


def build_backbone(args):
    position_embedding = build_position_encoding(args)

    # 是否训练backbone
    train_backbone = args.lr_backbone > 0

    # 是否返回中间层（中间的层也用一个head导出）
    return_interm_layers = args.masks

    # if args.batch_norm_type == 'FrozenBatchNorm2d':
    #     batch_norm = FrozenBatchNorm2d
    # elif args.batch_norm_type == 'SyncBatchNorm':
    #     batch_norm = nn.SyncBatchNorm
    # elif args.batch_norm_type == 'BatchNorm2d':
    #     batch_norm = nn.BatchNorm2d
    # else:
    #     raise NotImplementedError("Unknown batch norm name: {}".format(args.batch_norm_type))

    backbone = Backbone(args.backbone, train_backbone, return_interm_layers, args.dilation,
                        batch_norm=FrozenBatchNorm2d)
    model = Joiner(backbone, position_embedding)
    model.num_channels = backbone.num_channels
    return model
