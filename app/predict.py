import torch
import streamlit as st

import yaml
from typing import Tuple

import sys
sys.path.append('/opt/ml/input/VTO')
from HR_VITON.networks import ConditionGenerator, load_checkpoint, make_grid
import torchgeometry as tgm
import os
import time
from HR_VITON.utils import *
from torchvision.utils import make_grid as make_image_grid
from torchvision.utils import save_image
import io
from PIL import Image

def remove_overlap(seg_out, warped_cm):
    
    assert len(warped_cm.shape) == 4
    
    warped_cm = warped_cm - (torch.cat([seg_out[:, 1:3, :, :], seg_out[:, 5:, :, :]], dim=1)).sum(dim=1, keepdim=True) * warped_cm
    return warped_cm

def get_prediction(opt ,test_loader, tocg, generator, id) :
    gauss = tgm.image.GaussianBlur((15, 15), (3, 3))
    if opt.cuda:
        gauss = gauss.cuda()
    
    # Model
    if opt.cuda :
        tocg.cuda()
    tocg.eval()
    generator.eval()
    
    # if opt.output_dir is not None:
    output_dir = opt.output_dir
    # else:
    #     output_dir = os.path.join('./output', opt.test_name,
    #                         opt.datamode, opt.datasetting, 'generator', 'output')
    # grid_dir = os.path.join('./output', opt.test_name,
    #                          opt.datamode, opt.datasetting, 'generator', 'grid')
    
    # os.makedirs(grid_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    num = 0
    iter_start_time = time.time()
    with torch.no_grad():
        for inputs in test_loader.data_loader:
            if opt.cuda :
                pose_map = inputs['pose'].cuda()
                pre_clothes_mask = inputs['cloth_mask'][opt.datasetting].cuda()
                label = inputs['parse']
                parse_agnostic = inputs['parse_agnostic']
                agnostic = inputs['agnostic'].cuda()
                clothes = inputs['cloth'][opt.datasetting].cuda() # target cloth
                densepose = inputs['densepose'].cuda()
                im = inputs['image']
                input_label, input_parse_agnostic = label.cuda(), parse_agnostic.cuda()
                pre_clothes_mask = torch.FloatTensor((pre_clothes_mask.detach().cpu().numpy() > 0.5).astype(np.float)).cuda()
            else :
                pose_map = inputs['pose']
                pre_clothes_mask = inputs['cloth_mask'][opt.datasetting]
                label = inputs['parse']
                parse_agnostic = inputs['parse_agnostic']
                agnostic = inputs['agnostic']
                clothes = inputs['cloth'][opt.datasetting] # target cloth
                densepose = inputs['densepose']
                im = inputs['image']
                input_label, input_parse_agnostic = label, parse_agnostic
                pre_clothes_mask = torch.FloatTensor((pre_clothes_mask.detach().cpu().numpy() > 0.5).astype(np.float))

            # down
            pose_map_down = F.interpolate(pose_map, size=(256, 192), mode='bilinear')
            pre_clothes_mask_down = F.interpolate(pre_clothes_mask, size=(256, 192), mode='nearest')
            input_label_down = F.interpolate(input_label, size=(256, 192), mode='bilinear')
            input_parse_agnostic_down = F.interpolate(input_parse_agnostic, size=(256, 192), mode='nearest')
            agnostic_down = F.interpolate(agnostic, size=(256, 192), mode='nearest')
            clothes_down = F.interpolate(clothes, size=(256, 192), mode='bilinear')
            densepose_down = F.interpolate(densepose, size=(256, 192), mode='bilinear')

            shape = pre_clothes_mask.shape
            
            # multi-task inputs
            input1 = torch.cat([clothes_down, pre_clothes_mask_down], 1)
            input2 = torch.cat([input_parse_agnostic_down, densepose_down], 1)

            # forward
            flow_list, fake_segmap, warped_cloth_paired, warped_clothmask_paired = tocg(opt,input1, input2)
            
            # warped cloth mask one hot
            if opt.cuda :
                warped_cm_onehot = torch.FloatTensor((warped_clothmask_paired.detach().cpu().numpy() > 0.5).astype(np.float)).cuda()
            else :
                warped_cm_onehot = torch.FloatTensor((warped_clothmask_paired.detach().cpu().numpy() > 0.5).astype(np.float))

            if opt.clothmask_composition != 'no_composition':
                if opt.clothmask_composition == 'detach':
                    cloth_mask = torch.ones_like(fake_segmap)
                    cloth_mask[:,3:4, :, :] = warped_cm_onehot
                    fake_segmap = fake_segmap * cloth_mask
                    
                if opt.clothmask_composition == 'warp_grad':
                    cloth_mask = torch.ones_like(fake_segmap)
                    cloth_mask[:,3:4, :, :] = warped_clothmask_paired
                    fake_segmap = fake_segmap * cloth_mask
                    
            # make generator input parse map
            fake_parse_gauss = gauss(F.interpolate(fake_segmap, size=(opt.fine_height, opt.fine_width), mode='bilinear'))
            fake_parse = fake_parse_gauss.argmax(dim=1)[:, None]

            if opt.cuda :
                old_parse = torch.FloatTensor(fake_parse.size(0), 13, opt.fine_height, opt.fine_width).zero_().cuda()
            else:
                old_parse = torch.FloatTensor(fake_parse.size(0), 13, opt.fine_height, opt.fine_width).zero_()
            old_parse.scatter_(1, fake_parse, 1.0)

            labels = {
                0:  ['background',  [0]],
                1:  ['paste',       [2, 4, 7, 8, 9, 10, 11]],
                2:  ['upper',       [3]],
                3:  ['hair',        [1]],
                4:  ['left_arm',    [5]],
                5:  ['right_arm',   [6]],
                6:  ['noise',       [12]]
            }
            if opt.cuda :
                parse = torch.FloatTensor(fake_parse.size(0), 7, opt.fine_height, opt.fine_width).zero_().cuda()
            else:
                parse = torch.FloatTensor(fake_parse.size(0), 7, opt.fine_height, opt.fine_width).zero_()
            for i in range(len(labels)):
                for label in labels[i][1]:
                    parse[:, i] += old_parse[:, label]
                    
            # warped cloth
            N, _, iH, iW = clothes.shape
            flow = F.interpolate(flow_list[-1].permute(0, 3, 1, 2), size=(iH, iW), mode='bilinear').permute(0, 2, 3, 1)
            flow_norm = torch.cat([flow[:, :, :, 0:1] / ((96 - 1.0) / 2.0), flow[:, :, :, 1:2] / ((128 - 1.0) / 2.0)], 3)
            
            grid = make_grid(N, iH, iW,opt)
            warped_grid = grid + flow_norm
            warped_cloth = F.grid_sample(clothes, warped_grid, padding_mode='border')
            warped_clothmask = F.grid_sample(pre_clothes_mask, warped_grid, padding_mode='border')
            if opt.occlusion:
                warped_clothmask = remove_overlap(F.softmax(fake_parse_gauss, dim=1), warped_clothmask)
                warped_cloth = warped_cloth * warped_clothmask + torch.ones_like(warped_cloth) * (1-warped_clothmask)
            
            output = generator(torch.cat((agnostic, densepose, warped_cloth), dim=1), parse)
            # visualize
            unpaired_names = []
            for i in range(shape[0]):
                # grid = make_image_grid([(clothes[i].cpu() / 2 + 0.5), (pre_clothes_mask[i].cpu()).expand(3, -1, -1), visualize_segmap(parse_agnostic.cpu(), batch=i), ((densepose.cpu()[i]+1)/2),
                #                         (warped_cloth[i].cpu().detach() / 2 + 0.5), (warped_clothmask[i].cpu().detach()).expand(3, -1, -1), visualize_segmap(fake_parse_gauss.cpu(), batch=i),
                #                         (pose_map[i].cpu()/2 +0.5), (warped_cloth[i].cpu()/2 + 0.5), (agnostic[i].cpu()/2 + 0.5),
                #                         (im[i]/2 +0.5), (output[i].cpu()/2 +0.5)],
                #                         nrow=4)
                # unpaired_name = (inputs['c_name']['paired'][i].split('.')[0] + '_' + inputs['c_name'][opt.datasetting][i].split('.')[0] + '.png')
                unpaired_name = (id + '.png')
                # save_image(grid, os.path.join(grid_dir, unpaired_name))
                unpaired_names.append(unpaired_name)
                
            # save output
            save_images(output, unpaired_names, output_dir)
            # img = Image.open(output_dir + '/' + unpaired_names[0])
            # st.image(img)
            print(f"Test time {time.time() - iter_start_time}")


            # tensor = (output[0].clone() + 1) * 0.5 * 255
            # tensor = tensor.cpu().clamp(0, 255)

            # try:
            #     array = tensor.numpy().astype('uint8')
            # except:
            #     array = tensor.detach().numpy().astype('uint8')

            # if array.shape[0] == 1:
            #     array = array.squeeze(0)
            # elif array.shape[0] == 3:
            #     array = array.swapaxes(0, 1).swapaxes(1, 2)

            # im = Image.fromarray(array) # <PIL.Image.Image image mode=RGB size=768x1024 at 0x7FFAB4065FD0>
            # print(f"Test time {time.time() - iter_start_time}")
            # return im