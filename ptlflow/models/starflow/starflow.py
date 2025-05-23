from typing import Optional, Sequence

try:
    from spatial_correlation_sampler import SpatialCorrelationSampler
except ModuleNotFoundError:
    from ptlflow.utils.correlation import (
        IterSpatialCorrelationSampler as SpatialCorrelationSampler,
    )
import torch
import torch.nn as nn

from ptlflow.utils.registry import register_model
from .pwc_modules import conv, upsample2d_as, rescale_flow, initialize_msra
from .pwc_modules import WarpingLayer, FeatureExtractor
from .pwc_modules import FlowAndOccContextNetwork, FlowAndOccEstimatorDense
from .irr_modules import OccUpsampleNetwork, RefineFlow, RefineOcc
from ptlflow.models.base_model.base_model import BaseModel


class StarFlow(BaseModel):
    pretrained_checkpoints = {
        "things": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/starflow-things-d3966a93.ckpt",
        "sintel": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/starflow-sintel-3c06b844.ckpt",
        "kitti": "https://github.com/hmorimitsu/ptlflow/releases/download/weights1/starflow-kitti-34b9a3ed.ckpt",
    }

    def __init__(
        self,
        div_flow: float = 0.05,
        search_range: int = 4,
        output_level: int = 4,
        num_levels: int = 7,
        num_chs: Sequence[int] = (3, 16, 32, 64, 96, 128, 196),
        train_batch_size: Optional[int] = None,
        **kwargs,
    ):
        super(StarFlow, self).__init__(
            output_stride=64,
            loss_fn=None,
            **kwargs,
        )

        self.div_flow = div_flow
        self.search_range = search_range
        self.output_level = output_level
        self.num_levels = num_levels
        self.num_chs = num_chs
        self.train_batch_size = train_batch_size

        self.leakyRELU = nn.LeakyReLU(0.1, inplace=True)

        self.feature_pyramid_extractor = FeatureExtractor(self.num_chs)
        self.warping_layer = WarpingLayer()

        self.dim_corr = (self.search_range * 2 + 1) ** 2
        self.num_ch_in = self.dim_corr + 32 + 2 + 1

        self.flow_and_occ_estimators = FlowAndOccEstimatorDense(2 * self.num_ch_in)
        self.context_networks = FlowAndOccContextNetwork(
            2 * self.num_ch_in + 448 + 2 + 1
        )

        self.occ_shuffle_upsample = OccUpsampleNetwork(11, 1)

        self.conv_1x1 = nn.ModuleList(
            [
                conv(196, 32, kernel_size=1, stride=1, dilation=1),
                conv(128, 32, kernel_size=1, stride=1, dilation=1),
                conv(96, 32, kernel_size=1, stride=1, dilation=1),
                conv(64, 32, kernel_size=1, stride=1, dilation=1),
            ]
        )

        self.conv_1x1_1 = conv(16, 3, kernel_size=1, stride=1, dilation=1)

        self.conv_1x1_time = conv(
            2 * self.num_ch_in + 448,
            self.num_ch_in,
            kernel_size=1,
            stride=1,
            dilation=1,
        )

        self.refine_flow = RefineFlow(2 + 1 + 32)
        self.refine_occ = RefineOcc(1 + 32 + 32)

        self.corr = SpatialCorrelationSampler(
            kernel_size=1, patch_size=2 * self.search_range + 1, padding=0
        )

        initialize_msra(self.modules())

    def forward(self, inputs):
        images, image_resizer = self.preprocess_images(
            inputs["images"],
            bgr_add=0.0,
            bgr_mult=1.0,
            bgr_to_rgb=True,
            resize_mode="interpolation",
            interpolation_mode="bilinear",
            interpolation_align_corners=False,
        )
        list_imgs = [images[:, i] for i in range(images.shape[1])]

        _, _, height_im, width_im = list_imgs[0].size()

        # on the bottom level are original images
        list_pyramids = []  # indices : [time][level]
        for im in list_imgs:
            list_pyramids.append(self.feature_pyramid_extractor(im) + [im])

        # outputs
        output_dict = {}
        output_dict_eval = {}
        flows_f = []  # indices : [level][time]
        flows_b = []  # indices : [level][time]
        occs_f = []
        occs_b = []
        flows_coarse_f = []
        occs_coarse_f = []
        for l in range(len(list_pyramids[0])):
            flows_f.append([])
            flows_b.append([])
            occs_f.append([])
            occs_b.append([])
        for l in range(self.output_level + 1):
            flows_coarse_f.append([])
            occs_coarse_f.append([])

        # init
        (
            b_size,
            _,
            h_x1,
            w_x1,
        ) = list_pyramids[
            0
        ][0].size()
        init_dtype = list_pyramids[0][0].dtype
        init_device = list_pyramids[0][0].device
        flow_f = torch.zeros(
            b_size, 2, h_x1, w_x1, dtype=init_dtype, device=init_device
        )
        flow_b = torch.zeros(
            b_size, 2, h_x1, w_x1, dtype=init_dtype, device=init_device
        )
        occ_f = torch.zeros(b_size, 1, h_x1, w_x1, dtype=init_dtype, device=init_device)
        occ_b = torch.zeros(b_size, 1, h_x1, w_x1, dtype=init_dtype, device=init_device)
        previous_features = []

        for i in range(len(list_imgs) - 1):
            x1_pyramid, x2_pyramid = list_pyramids[i : i + 2]

            for l, (x1, x2) in enumerate(zip(x1_pyramid, x2_pyramid)):
                if l <= self.output_level:
                    if i == 0:
                        (
                            bs_,
                            _,
                            h_,
                            w_,
                        ) = list_pyramids[
                            0
                        ][l].size()
                        previous_features.append(
                            torch.zeros(
                                bs_,
                                self.num_ch_in,
                                h_,
                                w_,
                                dtype=init_dtype,
                                device=init_device,
                            )
                        )

                    # warping
                    if l == 0:
                        x2_warp = x2
                        x1_warp = x1
                    else:
                        flow_f = upsample2d_as(flow_f, x1, mode="bilinear")
                        flow_b = upsample2d_as(flow_b, x2, mode="bilinear")
                        occ_f = upsample2d_as(occ_f, x1, mode="bilinear")
                        occ_b = upsample2d_as(occ_b, x2, mode="bilinear")
                        x2_warp = self.warping_layer(
                            x2, flow_f, height_im, width_im, self.div_flow
                        )
                        x1_warp = self.warping_layer(
                            x1, flow_b, height_im, width_im, self.div_flow
                        )

                    # correlation
                    out_corr_f = self.corr(x1, x2_warp)
                    out_corr_f = out_corr_f.view(
                        out_corr_f.shape[0],
                        -1,
                        out_corr_f.shape[3],
                        out_corr_f.shape[4],
                    )
                    out_corr_f = out_corr_f / x1.shape[1]

                    out_corr_b = self.corr(x2, x1_warp)
                    out_corr_b = out_corr_b.view(
                        out_corr_b.shape[0],
                        -1,
                        out_corr_b.shape[3],
                        out_corr_b.shape[4],
                    )
                    out_corr_b = out_corr_b / x2.shape[1]

                    out_corr_relu_f = self.leakyRELU(out_corr_f)
                    out_corr_relu_b = self.leakyRELU(out_corr_b)

                    if l != self.output_level:
                        x1_1by1 = self.conv_1x1[l](x1)
                        x2_1by1 = self.conv_1x1[l](x2)
                    else:
                        x1_1by1 = x1
                        x2_1by1 = x2

                    if i > 0:  # temporal connection:
                        previous_features[l] = self.warping_layer(
                            previous_features[l],
                            flows_b[l][-1],
                            height_im,
                            width_im,
                            self.div_flow,
                        )

                    # Flow and occlusions estimation
                    flow_f = rescale_flow(
                        flow_f, self.div_flow, width_im, height_im, to_local=True
                    )
                    flow_b = rescale_flow(
                        flow_b, self.div_flow, width_im, height_im, to_local=True
                    )

                    features = torch.cat(
                        [previous_features[l], out_corr_relu_f, x1_1by1, flow_f, occ_f],
                        1,
                    )
                    features_b = torch.cat(
                        [
                            torch.zeros_like(previous_features[l]),
                            out_corr_relu_b,
                            x2_1by1,
                            flow_b,
                            occ_b,
                        ],
                        1,
                    )

                    x_intm_f, flow_res_f, occ_res_f = self.flow_and_occ_estimators(
                        features
                    )
                    flow_est_f = flow_f + flow_res_f
                    occ_est_f = occ_f + occ_res_f
                    with torch.no_grad():
                        x_intm_b, flow_res_b, occ_res_b = self.flow_and_occ_estimators(
                            features_b
                        )
                        flow_est_b = flow_b + flow_res_b
                        occ_est_b = occ_b + occ_res_b

                    # Context:
                    flow_cont_res_f, occ_cont_res_f = self.context_networks(
                        torch.cat([x_intm_f, flow_est_f, occ_est_f], dim=1)
                    )
                    flow_cont_f = flow_est_f + flow_cont_res_f
                    occ_cont_f = occ_est_f + occ_cont_res_f
                    with torch.no_grad():
                        flow_cont_res_b, occ_cont_res_b = self.context_networks(
                            torch.cat([x_intm_b, flow_est_b, occ_est_b], dim=1)
                        )
                        flow_cont_b = flow_est_b + flow_cont_res_b
                        occ_cont_b = occ_est_b + occ_cont_res_b

                    # refinement
                    img1_resize = upsample2d_as(list_imgs[i], flow_f, mode="bilinear")
                    img2_resize = upsample2d_as(
                        list_imgs[i + 1], flow_b, mode="bilinear"
                    )
                    flow_cont_f = rescale_flow(
                        flow_cont_f,
                        self.div_flow,
                        width_im,
                        height_im,
                        to_local=False,
                    )
                    flow_cont_b = rescale_flow(
                        flow_cont_b,
                        self.div_flow,
                        width_im,
                        height_im,
                        to_local=False,
                    )
                    img2_warp = self.warping_layer(
                        img2_resize,
                        flow_cont_f,
                        height_im,
                        width_im,
                        self.div_flow,
                    )
                    img1_warp = self.warping_layer(
                        img1_resize,
                        flow_cont_b,
                        height_im,
                        width_im,
                        self.div_flow,
                    )

                    # flow refine
                    flow_f = self.refine_flow(
                        flow_cont_f.detach(), img1_resize - img2_warp, x1_1by1
                    )
                    flow_b = self.refine_flow(
                        flow_cont_b.detach(), img2_resize - img1_warp, x2_1by1
                    )

                    flow_f = rescale_flow(
                        flow_f, self.div_flow, width_im, height_im, to_local=False
                    )
                    flow_b = rescale_flow(
                        flow_b, self.div_flow, width_im, height_im, to_local=False
                    )

                    # occ refine
                    x2_1by1_warp = self.warping_layer(
                        x2_1by1, flow_f, height_im, width_im, self.div_flow
                    )
                    x1_1by1_warp = self.warping_layer(
                        x1_1by1, flow_b, height_im, width_im, self.div_flow
                    )

                    occ_f = self.refine_occ(
                        occ_cont_f.detach(), x1_1by1, x1_1by1 - x2_1by1_warp
                    )
                    occ_b = self.refine_occ(
                        occ_cont_b.detach(), x2_1by1, x2_1by1 - x1_1by1_warp
                    )

                    # save features for temporal connection:
                    previous_features[l] = self.conv_1x1_time(x_intm_f)
                    flows_f[l].append(flow_f)
                    occs_f[l].append(occ_f)
                    flows_b[l].append(flow_b)
                    occs_b[l].append(occ_b)
                    flows_coarse_f[l].append(flow_cont_f)
                    occs_coarse_f[l].append(occ_cont_f)
                    # flows.append([flow_cont_f, flow_cont_b, flow_f, flow_b])
                    # occs.append([occ_cont_f, occ_cont_b, occ_f, occ_b])

                else:
                    flow_f = upsample2d_as(flow_f, x1, mode="bilinear")
                    flow_b = upsample2d_as(flow_b, x2, mode="bilinear")
                    flows_f[l].append(flow_f)
                    flows_b[l].append(flow_b)
                    # flows.append([flow_f, flow_b])

                    x2_warp = self.warping_layer(
                        x2, flow_f, height_im, width_im, self.div_flow
                    )
                    x1_warp = self.warping_layer(
                        x1, flow_b, height_im, width_im, self.div_flow
                    )
                    flow_b_warp = self.warping_layer(
                        flow_b, flow_f, height_im, width_im, self.div_flow
                    )
                    flow_f_warp = self.warping_layer(
                        flow_f, flow_b, height_im, width_im, self.div_flow
                    )

                    if l != self.num_levels - 1:
                        x1_in = self.conv_1x1_1(x1)
                        x2_in = self.conv_1x1_1(x2)
                        x1_w_in = self.conv_1x1_1(x1_warp)
                        x2_w_in = self.conv_1x1_1(x2_warp)
                    else:
                        x1_in = x1
                        x2_in = x2
                        x1_w_in = x1_warp
                        x2_w_in = x2_warp

                    occ_f = self.occ_shuffle_upsample(
                        occ_f, torch.cat([x1_in, x2_w_in, flow_f, flow_b_warp], dim=1)
                    )
                    occ_b = self.occ_shuffle_upsample(
                        occ_b, torch.cat([x2_in, x1_w_in, flow_b, flow_f_warp], dim=1)
                    )

                    occs_f[l].append(occ_f)
                    occs_b[l].append(occ_b)
                    # occs.append([occ_f, occ_b])

            flow_f = torch.zeros(
                b_size, 2, h_x1, w_x1, dtype=init_dtype, device=init_device
            )
            flow_b = torch.zeros(
                b_size, 2, h_x1, w_x1, dtype=init_dtype, device=init_device
            )
            occ_f = torch.zeros(
                b_size, 1, h_x1, w_x1, dtype=init_dtype, device=init_device
            )
            occ_b = torch.zeros(
                b_size, 1, h_x1, w_x1, dtype=init_dtype, device=init_device
            )

        flow_f_up = torch.stack(
            [
                upsample2d_as(f, list_imgs[0], mode="bilinear") / self.div_flow
                for f in flows_f[-1]
            ],
            dim=1,
        )
        flow_f_up = self.postprocess_predictions(flow_f_up, image_resizer, is_flow=True)
        flow_b_up = torch.stack(
            [
                upsample2d_as(f, list_imgs[0], mode="bilinear") / self.div_flow
                for f in flows_b[-1]
            ],
            dim=1,
        )
        flow_b_up = self.postprocess_predictions(flow_b_up, image_resizer, is_flow=True)
        occs_f_up = torch.stack(
            [
                upsample2d_as(torch.sigmoid(o), list_imgs[0], mode="bilinear")
                for o in occs_f[-1]
            ],
            dim=1,
        )
        occs_f_up = self.postprocess_predictions(
            occs_f_up, image_resizer, is_flow=False
        )
        occs_b_up = torch.stack(
            [
                upsample2d_as(torch.sigmoid(o), list_imgs[0], mode="bilinear")
                for o in occs_b[-1]
            ],
            dim=1,
        )
        occs_b_up = self.postprocess_predictions(
            occs_b_up, image_resizer, is_flow=False
        )

        outputs = {}

        if self.training:
            raise NotImplementedError("Training is still not implemented for StarFlow.")
        else:
            outputs["flows"] = flow_f_up
            outputs["occs"] = occs_f_up
            outputs["flows_b"] = flow_b_up
            outputs["occs_b"] = occs_b_up

        return outputs


@register_model
class starflow(StarFlow):
    pass
