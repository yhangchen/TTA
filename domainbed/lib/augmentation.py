import torch
import torch.nn as nn
import numpy as np
import random
from PIL import ImageFilter
from torchvision import transforms

class AugSimCLR(object):
    """Take two random crops of one image as the query and key."""

    def __init__(self, size, n_views=2):
        s = 1
        color_jitter = transforms.ColorJitter(0.8 * s, 0.8 * s, 0.8 * s, 0.2 * s)
        self.n_views = n_views
        p_blur = 0.5 if size > 32 else 0
        self.base_transform =  transforms.Compose([transforms.RandomResizedCrop(size=size, scale=(0.2, 1.0)),
                                            transforms.RandomHorizontalFlip(),
                                            transforms.RandomApply([color_jitter], p=0.8),
                                            transforms.RandomGrayscale(p=0.2),
                                            transforms.RandomApply([transforms.GaussianBlur(kernel_size=3)], p=p_blur),
                                            transforms.ToTensor(),
                                            transforms.Normalize([0.485, 0.456, 0.406],[0.229, 0.224, 0.225])])

    def __call__(self, x):
        return [self.base_transform(x) for _ in range(self.n_views)]

