import sys
import os

import numpy as np
import cntk as ct
from . import nn as nn

def loss_function(input_norm, target, z, loss):
    if loss == 'category':
        return softmax_256_loss(target, z)        
    elif loss == 'mixture':
        return discretized_mix_logistic_loss(input_norm,z)

    return None

def discretized_mix_logistic_loss(x,l):
    """
    Porting discretized_mix_logistic_loss from  https://github.com/openai/pixel-cnn/blob/master/pixel_cnn_pp/nn.py.

    log-likelihood for mixture of discretized logistics, assumes the data has been rescaled to [-1,1] interval
    """
    x = ct.transpose(ct.transpose(x,0,1),1,2) # From CHW to HWC
    l = ct.transpose(ct.transpose(l,0,1),1,2) # From CHW to HWC

    xs = x.shape # true image (i.e. labels) to regress to.
    ls = l.shape # predicted distribution.

    nr_mix = int(ls[-1] / 10) # here and below: unpacking the params of the mixture of logistics
    logit_probs = l[:,:,:nr_mix]
    l = ct.reshape(l[:,:,nr_mix:100], xs + (nr_mix*3,))
    means = l[:,:,:,:nr_mix]
    log_scales = nn.maximum(l[:,:,:,nr_mix:2*nr_mix], -7.)
    coeffs = ct.tanh(l[:,:,:,2*nr_mix:3*nr_mix])
    x = ct.reshape(x, xs + (1,)) + ct.constant(value=0., shape=xs + (nr_mix,)) # tf.zeros(xs + [nr_mix]) # here and below: getting the means and adjusting them based on preceding sub-pixels
    m2 = ct.reshape(means[:,:,1,:] + coeffs[:, :, 0, :] * x[:, :, 0, :], (xs[0],xs[1],1,nr_mix))
    m3 = ct.reshape(means[:, :, 2, :] + coeffs[:, :, 1, :] * x[:, :, 0, :] + coeffs[:, :, 2, :] * x[:, :, 1, :], (xs[0],xs[1],1,nr_mix))
    means = ct.splice(ct.reshape(means[:,:,0,:], (xs[0],xs[1],1,nr_mix)), m2, m3, axis=2)
    centered_x = x - means
    inv_stdv = ct.exp(-log_scales)
    plus_in = inv_stdv * (centered_x + 1./255.)
    cdf_plus = ct.sigmoid(plus_in)
    min_in = inv_stdv * (centered_x - 1./255.)
    cdf_min = ct.sigmoid(min_in)
    log_cdf_plus = plus_in - nn.softplus(plus_in) # log probability for edge case of 0 (before scaling)
    log_one_minus_cdf_min = -nn.softplus(min_in) # log probability for edge case of 255 (before scaling)
    cdf_delta = cdf_plus - cdf_min # probability for all other cases
    mid_in = inv_stdv * centered_x
    log_pdf_mid = mid_in - log_scales - ct.constant(2) * nn.softplus(mid_in) # log probability in the center of the bin, to be used in extreme cases (not actually used in our code)

    # now select the right output: left edge case, right edge case, normal case, extremely low prob case (doesn't actually happen for us)

    # this is what we are really doing, but using the robust version below for extreme cases in other applications and to avoid NaN issue with tf.select()
    # log_probs = tf.select(x < -0.999, log_cdf_plus, tf.select(x > 0.999, log_one_minus_cdf_min, tf.log(cdf_delta)))

    # robust version, that still works if probabilities are below 1e-5 (which never happens in our code)
    # tensorflow backpropagates through tf.select() by multiplying with zero instead of selecting: this requires use to use some ugly tricks to avoid potential NaNs
    # the 1e-12 in tf.maximum(cdf_delta, 1e-12) is never actually used as output, it's purely there to get around the tf.select() gradient issue
    # if the probability on a sub-pixel is below 1e-5, we use an approximation based on the assumption that the log-density is constant in the bin of the observed sub-pixel value
    log_probs = ct.element_select(ct.less(x, ct.constant(-0.999)), 
                                  log_cdf_plus, 
                                  ct.element_select(ct.greater(x, ct.constant(0.999)), 
                                                    log_one_minus_cdf_min, 
                                                    ct.element_select(ct.greater(cdf_delta, 1e-5), 
                                                                      ct.log(nn.maximum(cdf_delta, 1e-12)), 
                                                                      log_pdf_mid - np.log(127.5))))

    log_probs = ct.reshape(ct.reduce_sum(log_probs, axis=2), shape=logit_probs.shape)
    log_probs = log_probs + nn.log_prob_from_logits(logit_probs)

    losses = nn.log_sum_exp(log_probs)
    loss = ct.reshape(-ct.reduce_sum(ct.reduce_sum(losses,axis=0),axis=1), shape=(1,))
    return loss

def sample_from_discretized_mix_logistic(l,nr_mix):
    l = ct.transpose(ct.transpose(l,0,1),1,2) # From CHW to HWC
    ls = l.shape
    xs = ls[:-1] + (3,)

    # unpack parameters
    logit_probs = l[:, :, :nr_mix]
    l = ct.reshape(l[:, :, nr_mix:], xs + (nr_mix*3,))

    # sample mixture indicator from softmax
    sel = tf.one_hot(tf.argmax(logit_probs - tf.log(-tf.log(tf.random_uniform(logit_probs.get_shape(), minval=1e-5, maxval=1. - 1e-5))), 3), depth=nr_mix, dtype=tf.float32)



    sel = ct.reshape(sel, xs[:-1] + (1,nr_mix))
    # select logistic parameters
    means = ct.reduce_sum(l[:,:,:,:nr_mix]*sel,4)
    log_scales = nn.maximum(ct.reduce_sum(l[:,:,:,nr_mix:2*nr_mix]*sel,4), -7.)
    coeffs = ct.reduce_sum(ct.tanh(l[:,:,:,2*nr_mix:3*nr_mix])*sel,4)

    # sample from logistic & clip to interval
    # we don't actually round to the nearest 8bit value when sampling
    u = tf.random_uniform(means.get_shape(), minval=1e-5, maxval=1. - 1e-5)


    x = means + ct.exp(log_scales)*(ct.log(u) - ct.log(1. - u))
    x0 = nn.minimum(nn.maximum(x[:,:,0], -1.), 1.)
    x1 = nn.minimum(nn.maximum(x[:,:,1] + coeffs[:,:,0]*x0, -1.), 1.)
    x2 = nn.minimum(nn.maximum(x[:,:,2] + coeffs[:,:,1]*x0 + coeffs[:,:,2]*x1, -1.), 1.)

    image = ct.splice(ct.reshape(x0,xs[:-1]+(1,)), ct.reshape(x1,xs[:-1]+(1,)), ct.reshape(x2,xs[:-1]+(1,)), axis=3)

    return ct.reshape(image, (3,32,32))

def softmax_256_loss(image_target, prediction):
    # Cross Entropy.

    # image_target: (256, 3*32*32)
    # predication: (3x256, 32, 32)
    image_pred = ct.reshape(prediction, (256, 3*32*32))
    train_loss = ct.reduce_sum(ct.ops.cross_entropy_with_softmax(image_pred, image_target, axis=0))
    # train_loss = ct.minus(ct.reduce_log_sum(image_pred, axis=-2), ct.reduce_sum(ct.element_times(image_target,image_pred), axis=-2))

    return train_loss