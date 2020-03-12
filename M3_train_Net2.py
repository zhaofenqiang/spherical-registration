#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Mar  3 12:44:31 2020

@author: fenqiang
"""


import torch

import torchvision
import numpy as np
import glob
import math

from utils import Get_neighs_order, get_z_weight, get_vertex_dis
from utils_vtk import read_vtk
from utils_torch import resampleSphereSurf, bilinearResampleSphereSurf, bilinearResampleSphereSurf_v2, getEn
from tensorboardX import SummaryWriter
writer = SummaryWriter('log/M3_2')

from model import Unet

###########################################################
""" hyper-parameters """

device = torch.device('cuda:0') # torch.device('cpu'), or torch.device('cuda:0')
learning_rate = 0.0005
weight_corr = 1.0
weight_smooth = 15.0
weight_l2 = 10.0
weight_l1 = 0.0
weight_phi_consis = 10.0
regis_feat = 'sulc' # 'sulc' or 'curv'
num_composition = 6

truncated = False
norm_method = '2' # '1': use individual max min, '2': use fixed max min
n_vertex = 40962
bi = True

###########################################################

in_ch = 2   # one for sulc in fixed, one for sulc in moving
out_ch = 2  # two components for tangent plane deformation vector 
batch_size = 1
data_for_test = 0.3
max_disp = get_vertex_dis(n_vertex)/100.0 * 0.3

###########################################################
""" split files, only need 18 month now"""

files = sorted(glob.glob('/media/fenqiang/DATA/unc/Data/registration/data/preprocessed_npy/*/*.lh.SphereSurf.Orig.sphere.resampled.'+str(n_vertex)+'.npy'))
files = [x for x in files if float(x.split('/')[-1].split('_')[1].split('.')[0]) >=450 and float(x.split('/')[-1].split('_')[1].split('.')[0]) <= 630]

test_files = [ files[x] for x in range(int(len(files)*data_for_test)) ]
train_files = [ files[x] for x in range(int(len(files)*data_for_test), len(files)) ]

###########################################################
""" load fixed/atlas surface, smooth filter, global parameter pre-defined """

def get_bi_inter(n_vertex, device):
    inter_indices_0 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_indices_'+ str(n_vertex) +'_0.npy')
    inter_indices_0 = torch.from_numpy(inter_indices_0.astype(np.int64)).to(device)
    inter_weights_0 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_weights_'+ str(n_vertex) +'_0.npy')
    inter_weights_0 = torch.from_numpy(inter_weights_0.astype(np.float32)).to(device)
    
    inter_indices_1 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_indices_'+ str(n_vertex) +'_1.npy')
    inter_indices_1 = torch.from_numpy(inter_indices_1.astype(np.int64)).to(device)
    inter_weights_1 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_weights_'+ str(n_vertex) +'_1.npy')
    inter_weights_1 = torch.from_numpy(inter_weights_1.astype(np.float32)).to(device)
    
    inter_indices_2 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_indices_'+ str(n_vertex) +'_2.npy')
    inter_indices_2 = torch.from_numpy(inter_indices_2.astype(np.int64)).to(device)
    inter_weights_2 = np.load('/media/fenqiang/DATA/unc/Data/registration/scripts/neigh_indices/img_weights_'+ str(n_vertex) +'_2.npy')
    inter_weights_2 = torch.from_numpy(inter_weights_2.astype(np.float32)).to(device)
    
    return (inter_indices_0, inter_weights_0), (inter_indices_1, inter_weights_1), (inter_indices_2, inter_weights_2)


def get_latlon_img(bi_inter, feat):
    inter_indices, inter_weights = bi_inter
    width = int(np.sqrt(len(inter_indices)))
    img = torch.sum(((feat[inter_indices.flatten()]).reshape(inter_indices.shape[0], inter_indices.shape[1], feat.shape[1])) * ((inter_weights.unsqueeze(2)).repeat(1,1,feat.shape[1])), 1)
    img = img.reshape(width, width, feat.shape[1])
    
    return img


def getOverlapIndex(n_vertex, device):
    """
    Compute the overlap indices' index for the 3 deforamtion field
    """
    z_weight_0 = get_z_weight(n_vertex, 0)
    z_weight_0 = torch.from_numpy(z_weight_0.astype(np.float32)).cuda(device)
    index_0_0 = (z_weight_0 == 1).nonzero()
    index_0_1 = (z_weight_0 < 1).nonzero()
    assert len(index_0_0) + len(index_0_1) == n_vertex, "error!"
    z_weight_1 = get_z_weight(n_vertex, 1)
    z_weight_1 = torch.from_numpy(z_weight_1.astype(np.float32)).cuda(device)
    index_1_0 = (z_weight_1 == 1).nonzero()
    index_1_1 = (z_weight_1 < 1).nonzero()
    assert len(index_1_0) + len(index_1_1) == n_vertex, "error!"
    z_weight_2 = get_z_weight(n_vertex, 2)
    z_weight_2 = torch.from_numpy(z_weight_2.astype(np.float32)).cuda(device)
    index_2_0 = (z_weight_2 == 1).nonzero()
    index_2_1 = (z_weight_2 < 1).nonzero()
    assert len(index_2_0) + len(index_2_1) == n_vertex, "error!"
    
    index_01 = np.intersect1d(index_0_0.detach().cpu().numpy(), index_1_0.detach().cpu().numpy())
    index_02 = np.intersect1d(index_0_0.detach().cpu().numpy(), index_2_0.detach().cpu().numpy())
    index_12 = np.intersect1d(index_1_0.detach().cpu().numpy(), index_2_0.detach().cpu().numpy())
    index_01 = torch.from_numpy(index_01).cuda(device)
    index_02 = torch.from_numpy(index_02).cuda(device)
    index_12 = torch.from_numpy(index_12).cuda(device)
    rot_mat_01 = torch.tensor([[np.cos(np.pi/2), 0, np.sin(np.pi/2)],
                               [0., 1., 0.],
                               [-np.sin(np.pi/2), 0, np.cos(np.pi/2)]]).cuda(device)
    rot_mat_12 = torch.tensor([[1., 0., 0.],
                               [0, np.cos(np.pi/2), -np.sin(np.pi/2)],
                               [0, np.sin(np.pi/2), np.cos(np.pi/2)]]).cuda(device)
    rot_mat_02 = torch.mm(rot_mat_12, rot_mat_01)
    rot_mat_20 = torch.inverse(rot_mat_02)
    
    tmp = torch.cat((index_0_0, index_1_0, index_2_0))
    tmp, indices = torch.sort(tmp.squeeze())
    output, counts = torch.unique_consecutive(tmp, return_counts=True)
    assert len(output) == n_vertex, "len(output) = n_vertex, error"
    assert output[0] == 0, "output[0] = 0, error"
    assert output[-1] == n_vertex-1, "output[-1] = n_vertex-1, error"
    assert counts.max() == 3, "counts.max() == 3, error"
    assert counts.min() == 2, "counts.min() == 3, error"
    index_triple_computed = (counts == 3).nonzero().squeeze()
    tmp = np.intersect1d(index_02.cpu().numpy(), index_triple_computed.cpu().numpy())
    assert (tmp == index_triple_computed.cpu().numpy()).all(), "(tmp == index_triple_computed.cpu().numpy()).all(), error"
    index_double_02 = torch.from_numpy(np.setdiff1d(index_02.cpu().numpy(), index_triple_computed.cpu().numpy())).cuda(device)
    tmp = np.intersect1d(index_12.cpu().numpy(), index_triple_computed.cpu().numpy())
    assert (tmp == index_triple_computed.cpu().numpy()).all(), "(tmp == index_triple_computed.cpu().numpy()).all(), error"
    index_double_12 = torch.from_numpy(np.setdiff1d(index_12.cpu().numpy(), index_triple_computed.cpu().numpy())).cuda(device)
    tmp = np.intersect1d(index_01.cpu().numpy(), index_triple_computed.cpu().numpy())
    assert (tmp == index_triple_computed.cpu().numpy()).all(), "(tmp == index_triple_computed.cpu().numpy()).all(), error"
    index_double_01 = torch.from_numpy(np.setdiff1d(index_01.cpu().numpy(), index_triple_computed.cpu().numpy())).cuda(device)
    assert len(index_double_01) + len(index_double_12) + len(index_double_02) + len(index_triple_computed) == n_vertex, "double computed and three computed error"

    return rot_mat_01, rot_mat_12, rot_mat_02, rot_mat_20, z_weight_0, z_weight_1, z_weight_2, index_01, index_12, index_02, index_0_0, index_1_0, index_2_0, index_double_02, index_double_12, index_double_01, index_triple_computed


def get_atlas(n_vertex, regis_feat, norm_method, device):
    fixed_0 = read_vtk('/media/fenqiang/DATA/unc/Data/Template/Atlas-20200107-newsulc/18/18.lh.SphereSurf.'+str(n_vertex)+'.rotated_0.vtk')
    
    if regis_feat == 'sulc':
        fixed_sulc = fixed_0['sulc']
    elif regis_feat == 'curv':
        fixed_sulc = fixed_0['curv']
    else:
        raise NotImplementedError('feat should be curv or sulc.')
        
    if norm_method == '1':
        fixed_sulc = (fixed_sulc - fixed_sulc.min())/(fixed_sulc.max()-fixed_sulc.min()) * 2. - 1.
    elif norm_method == '2':
        if regis_feat == 'sulc':
            fixed_sulc = (fixed_sulc + 11.5)/(13.65+11.5)
        else:
            fixed_sulc = (fixed_sulc + 2.32)/(2.08+2.32)
    else:
        raise NotImplementedError('norm_method should be 1 or 2.')
    
    fixed_sulc = fixed_sulc[:, np.newaxis]
    fixed_sulc = torch.from_numpy(fixed_sulc.astype(np.float32)).cuda(device)
    
    fixed_xyz_0 = fixed_0['vertices']/100.0  # fixed spherical coordinate
    fixed_xyz_0 = torch.from_numpy(fixed_xyz_0.astype(np.float32)).cuda(device)
    
    fixed_1 = read_vtk('/media/fenqiang/DATA/unc/Data/Template/Atlas-20200107-newsulc/18/18.lh.SphereSurf.'+str(n_vertex)+'.rotated_1.vtk')
    fixed_xyz_1 = fixed_1['vertices']/100.0  # fixed spherical coordinate
    fixed_xyz_1 = torch.from_numpy(fixed_xyz_1.astype(np.float32)).cuda(device)
    
    fixed_2 = read_vtk('/media/fenqiang/DATA/unc/Data/Template/Atlas-20200107-newsulc/18/18.lh.SphereSurf.'+str(n_vertex)+'.rotated_2.vtk')
    fixed_xyz_2 = fixed_2['vertices']/100.0  # fixed spherical coordinate
    fixed_xyz_2 = torch.from_numpy(fixed_xyz_2.astype(np.float32)).cuda(device)
    
    return fixed_xyz_0, fixed_xyz_1, fixed_xyz_2, fixed_sulc


############################################################################
    
fixed_xyz_0, fixed_xyz_1, fixed_xyz_2, fixed_sulc = get_atlas(n_vertex, regis_feat, norm_method, device)

grad_filter = torch.ones((7, 1), dtype=torch.float32, device = device)
grad_filter[6] = -6 

ns_vertex = np.array([163842,40962,10242,2562,642,162,42,12])
level = 8 - np.nonzero(ns_vertex-n_vertex == 0)[0][0]
n_res = level-1 if level<6 else 5

neigh_orders = Get_neighs_order(0)[8-level]
neigh_orders = torch.from_numpy(neigh_orders).to(device)
assert len(neigh_orders) == n_vertex * 7, "neigh_orders wrong!"

En_0, En_1, En_2 = getEn(n_vertex, device)

rot_mat_01, rot_mat_12, rot_mat_02, rot_mat_20, z_weight_0, z_weight_1, z_weight_2, index_01, index_12, index_02, index_0_0, index_1_0, index_2_0, index_double_02, index_double_12, index_double_01, index_triple_computed = getOverlapIndex(n_vertex, device)
bi_inter_0, bi_inter_1, bi_inter_2 = get_bi_inter(n_vertex, device)
img0 = get_latlon_img(bi_inter_0, fixed_sulc)
img1 = get_latlon_img(bi_inter_1, fixed_sulc)
img2 = get_latlon_img(bi_inter_2, fixed_sulc)

#img0 = torch.transpose(img0, 0, 2).transpose(1, 2).unsqueeze(0)
#img1 = torch.transpose(img1, 0, 2).transpose(1, 2).unsqueeze(0)
#img2 = torch.transpose(img2, 0, 2).transpose(1, 2).unsqueeze(0)


#############################################################

class BrainSphere(torch.utils.data.Dataset):

    def __init__(self, files, regis_feat, norm_method):
        self.files = files
        self.regis_feat = regis_feat
        self.norm_method = norm_method

    def __getitem__(self, index):
        file = self.files[index]
        data = np.load(file)
        if self.regis_feat == 'sulc':
            sulc = data[:,1]
        else:
            sulc = data[:,0]
            
        if self.norm_method == '1':
            sulc = (sulc - sulc.min())/(sulc.max()-sulc.min()) * 2. - 1.
        else:
            if self.regis_feat == 'sulc':
                sulc = (sulc + 11.5)/(13.65+11.5)
            else:
                sulc = (sulc + 2.32)/(2.08+2.32)
            
        return sulc.astype(np.float32)

    def __len__(self):
        return len(self.files)

train_dataset = BrainSphere(train_files, regis_feat, norm_method)
train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True, pin_memory=True)
#val_dataset = BrainSphere(test_files)
#val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=batch_size, shuffle=False, pin_memory=True)

model_0 = Unet(in_ch=in_ch, out_ch=out_ch, level=level, n_res=n_res, rotated=0)
model_0.to(device)
optimizer_0 = torch.optim.Adam(model_0.parameters(), lr=learning_rate,  betas=(0.9, 0.999))

model_1 = Unet(in_ch=in_ch, out_ch=out_ch, level=level, n_res=n_res, rotated=1)
model_1.to(device)
optimizer_1 = torch.optim.Adam(model_1.parameters(), lr=learning_rate,  betas=(0.9, 0.999))

model_2 = Unet(in_ch=in_ch, out_ch=out_ch, level=level, n_res=n_res, rotated=2)
model_2.to(device)
optimizer_2 = torch.optim.Adam(model_2.parameters(), lr=learning_rate,  betas=(0.9, 0.999))

optimizers = [optimizer_0, optimizer_1, optimizer_2]


def get_learning_rate(epoch):
    limits = [5, 15, 30]
    lrs = [1, 0.5, 0.05, 0.005]
    assert len(lrs) == len(limits) + 1
    for lim, lr in zip(limits, lrs):
        if epoch < lim:
            return lr * learning_rate
    return lrs[-1] * learning_rate


def diffeomorp(fixed_xyz, phi_3d, num_composition=6, bi=False, bi_inter=None, neigh_orders=None, device=None):
    if bi:
        assert bi_inter is not None, "bi_inter is None!"
        
    warped_vertices = fixed_xyz + phi_3d
    warped_vertices = warped_vertices/(torch.norm(warped_vertices, dim=1, keepdim=True).repeat(1,3))
    
    # compute exp
    for i in range(num_composition):
        if bi:
            warped_vertices = bilinearResampleSphereSurf_v2(warped_vertices, warped_vertices.clone(), bi_inter)
        else:
            warped_vertices = resampleSphereSurf(fixed_xyz, warped_vertices, warped_vertices, neigh_orders, device)
        
        warped_vertices = warped_vertices/(torch.norm(warped_vertices, dim=1, keepdim=True).repeat(1,3))
    
    return warped_vertices


def convert2DTo3D(phi_2d, En):
    """
    phi_2d: N*2
    En: N*6
    """
    phi_3d = torch.zeros(len(En), 3).to(device)
    tmp = En * phi_2d.repeat(1,3)
    phi_3d[:,0] = tmp[:,0] + tmp[:,1]
    phi_3d[:,1] = tmp[:,2] + tmp[:,3]
    phi_3d[:,2] = tmp[:,4] + tmp[:,5]
    return phi_3d


for epoch in range(80):
    lr = get_learning_rate(epoch)
    for optimizer in optimizers:
        optimizer.param_groups[0]['lr'] = lr
    print("learning rate = {}".format(lr))
    
#    dataiter = iter(train_dataloader)
#    moving_0 = dataiter.next()
    
    for batch_idx, (moving_0) in enumerate(train_dataloader):
        
        model_0.train()
        model_1.train()
        model_2.train()
        
        moving = torch.transpose(moving_0, 0, 1).to(device)
        data = torch.cat((moving, fixed_sulc), 1)
        
        # tangent vector field phi
        phi_2d_0_orig = model_0(data)/7.0
        phi_2d_1_orig = model_1(data)/7.0
        phi_2d_2_orig = model_2(data)/7.0
        
        phi_3d_0_orig = convert2DTo3D(phi_2d_0_orig, En_0)
        phi_3d_1_orig = convert2DTo3D(phi_2d_1_orig, En_1)
        phi_3d_2_orig = convert2DTo3D(phi_2d_2_orig, En_2)
        
        """ deformation consistency  """
        phi_3d_0_to_1 = torch.mm(rot_mat_01, torch.transpose(phi_3d_0_orig, 0, 1))
        phi_3d_0_to_1 = torch.transpose(phi_3d_0_to_1, 0, 1)
        phi_3d_1_to_2 = torch.mm(rot_mat_12, torch.transpose(phi_3d_1_orig, 0, 1))
        phi_3d_1_to_2 = torch.transpose(phi_3d_1_to_2, 0, 1)
        phi_3d_0_to_2 = torch.mm(rot_mat_02, torch.transpose(phi_3d_0_orig, 0, 1))
        phi_3d_0_to_2 = torch.transpose(phi_3d_0_to_2, 0, 1)
        
        # merge 
        phi_3d = torch.zeros(len(En_0), 3).cuda(device)
        phi_3d[index_double_02] = (phi_3d_0_to_2[index_double_02] + phi_3d_2_orig[index_double_02])/2.0
        phi_3d[index_double_12] = (phi_3d_1_to_2[index_double_12] + phi_3d_2_orig[index_double_12])/2.0
        tmp = (phi_3d_0_to_1[index_double_01] + phi_3d_1_orig[index_double_01])/2.0
        phi_3d[index_double_01] = torch.transpose(torch.mm(rot_mat_12, torch.transpose(tmp,0,1)), 0, 1)
        phi_3d[index_triple_computed] = (phi_3d_1_to_2[index_triple_computed] + phi_3d_2_orig[index_triple_computed] + phi_3d_0_to_2[index_triple_computed])/3.0
        phi_3d = torch.transpose(torch.mm(rot_mat_20, torch.transpose(phi_3d,0,1)),0,1)
        
        # divide to small veloctiy field
        phi_3d = phi_3d/math.pow(2,num_composition)
        print(torch.norm(phi_3d,dim=1).max().item())
        
        # truncate
        if truncated:
            tmp = torch.norm(phi_3d, dim=1) > max_disp
            phi_3d_tmp = phi_3d.clone()
            phi_3d_tmp[tmp] = phi_3d[tmp] / (torch.norm(phi_3d[tmp], dim=1, keepdim=True).repeat(1,3)) * max_disp
            phi_3d = phi_3d_tmp
        
        moving_warp_phi_3d = diffeomorp(fixed_xyz_0, phi_3d, num_composition=num_composition, bi=bi, bi_inter=bi_inter_0, neigh_orders=neigh_orders, device=device)
         
        """ compute interpolation values on fixed surface """
        if bi:
            fixed_inter = bilinearResampleSphereSurf(moving_warp_phi_3d, img0)
        else:
            fixed_inter = resampleSphereSurf(fixed_xyz_0, moving_warp_phi_3d, fixed_sulc, neigh_orders, device)
        
        
        loss_l1 = torch.mean(torch.abs(fixed_inter - moving))
                  
        loss_corr = 1 - ((fixed_inter - fixed_inter.mean()) * (moving - moving.mean())).mean() / fixed_inter.std() / moving.std()
                   
        loss_l2 = torch.mean((fixed_inter - moving)**2)
                  
        tmp_0 = torch.abs(torch.mm(phi_3d_0_orig[:,[0]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_0.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_0_orig[:,[1]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_0.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_0_orig[:,[2]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_0.unsqueeze(1)
        tmp_1 = torch.abs(torch.mm(phi_3d_1_orig[:,[0]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_1.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_1_orig[:,[1]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_1.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_1_orig[:,[2]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_1.unsqueeze(1)
        tmp_2 = torch.abs(torch.mm(phi_3d_2_orig[:,[0]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_2.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_2_orig[:,[1]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_2.unsqueeze(1) + \
                torch.abs(torch.mm(phi_3d_2_orig[:,[2]][neigh_orders].view(n_vertex, 7), grad_filter)) * z_weight_2.unsqueeze(1)
        loss_smooth = torch.mean(tmp_0) + torch.mean(tmp_1) + torch.mean(tmp_2)
        
        loss_phi_consistency = torch.mean(torch.abs(phi_3d_0_to_1[index_01] - phi_3d_1_orig[index_01])) + \
                               torch.mean(torch.abs(phi_3d_1_to_2[index_12] - phi_3d_2_orig[index_12])) + \
                               torch.mean(torch.abs(phi_3d_0_to_2[index_02] - phi_3d_2_orig[index_02]))
         
        loss = weight_l1 * loss_l1 + weight_smooth * loss_smooth + weight_l2 * loss_l2 + weight_phi_consis * loss_phi_consistency + weight_corr * loss_corr
    
        for optimizer in optimizers:
            optimizer.zero_grad()
        loss.backward()
        for optimizer in optimizers:
            optimizer.step()
       
        print("[Epoch {}] [Batch {}/{}] [loss_l1: {:5.4f}] [loss_l2: {:5.4f}] [loss_corr: {:5.4f}] [loss_smooth: {:5.4f}] [loss_phi_consistency: {:5.4f}]".format(epoch, batch_idx, len(train_dataloader),
                                                            loss_l1.item(), loss_l2.item(), loss_corr.item(), loss_smooth.item(), loss_phi_consistency.item()))
        writer.add_scalars('Train/loss', {'loss_l1': loss_l1.item()*weight_l1, 
                                          'loss_l2': loss_l2.item()*weight_l2,
                                          'loss_corr': loss_corr.item()*weight_corr, 
                                          'loss_smooth': loss_smooth.item()*weight_smooth, 
                                          'loss_phi_consistency': loss_phi_consistency.item()*weight_phi_consis}, 
                                          epoch*len(train_dataloader) + batch_idx)
    
    torch.save(model_0.state_dict(), "/media/fenqiang/DATA/unc/Data/registration/scripts/trained_model/M3_2_regis_"+regis_feat+"_"+str(n_vertex)+"_3d_smooth10_phiconsis1_corr0p1_0.mdl")
    torch.save(model_1.state_dict(), "/media/fenqiang/DATA/unc/Data/registration/scripts/trained_model/M3_2_regis_"+regis_feat+"_"+str(n_vertex)+"_3d_smooth10_phiconsis1_corr0p1_1.mdl")
    torch.save(model_2.state_dict(), "/media/fenqiang/DATA/unc/Data/registration/scripts/trained_model/M3_2_regis_"+regis_feat+"_"+str(n_vertex)+"_3d_smooth10_phiconsis1_corr0p1_2.mdl")
    
    