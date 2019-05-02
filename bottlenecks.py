import torch
from torch import nn
from torch import distributions as dist
import rfield

class LCCombine(nn.Module):
    '''The bottleneck loss terms for VAE and VQVAE are based on "z", which, for
    a single sample, consists of ~14 or so individual timesteps.  This enables
    the proper accounting of commitment loss terms when batching training
    samples across the time dimension.  
    '''
    def __init__(self, name=None):
        super(LCCombine, self).__init__()
        self.name = name

    def set_geometry(self, beg_rf, end_rf):
        self.rf = rfield.condensed(beg_rf, end_rf, self.name) 
        self.rf.gen_stats(1, self.rf)
        stride = self.rf.stride_ratio.denominator
        l_off, r_off = rfield.offsets(self.rf, self.rf)
        filter_sz = l_off - r_off + 1
        # pad_add = kernel_size - 1 - pad_arg (see torch.nn.ConvTranspose1d)
        # => pad_arg = kernel_size - 1 - pad_add 
        pad_add = max(self.rf.l_pad, self.rf.r_pad)
        self.l_trim = pad_add - self.rf.l_pad
        self.r_trim = pad_add - self.rf.r_pad
        pad_arg = filter_sz - 1 - pad_add
        self.tconv = nn.ConvTranspose1d(1, 1, filter_sz, stride, pad_arg, bias=False)

    def forward(self, z_metric):
        '''
        B, T, S: batch_sz, timesteps, less-frequent timesteps
        D: number of 
        z_metric: B, S, 1 
        output: B, T, 1
        '''
        out = self.tconv(z_metric)
        out_trim = out[:,self.l_trim:-self.r_trim or None,:]
        return out_trim


class VQVAE(nn.Module):
    def __init__(self, n_in, n_out, bias=True):
        super(VQVAE, self).__init__()
        self.proto_vec = 0
        self.linear = nn.Conv1d(n_in, n_out, 1, bias=bias)
        self.nearest_proto()

    def forward(self, x):
        '''
        vq_norms[:,:,0] = ||sg(z_{e}(x)) - e_{q(x)}||^2
        vq_norms[:,:,1] = ||z_{e}(x) - sg(e_{q(x)})||^2

        output[b,t,0] is the sum of the first vq_norm corresponding to the
        conditioning vectors used by WaveNet's output timestep t.  It is used
        as the total commitment loss for sample t
        '''
        out = self.linear(x)
        out = self.nearest_proto(out)
        return out


class VAE(nn.Module):
    def __init__(self, n_in, n_out, n_sam_per_datapoint=1, bias=True):
        '''n_sam_per_datapoint is L from equation 7,
        https://arxiv.org/pdf/1312.6114.pdf'''
        super(VAE, self).__init__()
        self.linear = nn.Conv1d(n_in, n_out * 2, 1, bias=bias)
        self.n_sam_per_datapoint = n_sam_per_datapoint

        # Cache these values for later access by the objective function
        self.mu = None
        self.sigma = None

    def forward(self, x):
        # B, T, I, C: n_batch, n_timesteps, n_in_chan, n_out_chan
        # L: n_sam_per_datapoint
        # Input: (B, I, T)
        # Output: (B * L, C, T)
        mu_sigma = self.linear(x)
        n_out_chan = mu_sigma.size(1) // 2
        mu = mu_sigma[:,:n_out_chan,:]  # first half of the channels
        sigma = mu_sigma[:,n_out_chan:,:] # second half of the channels

        L = self.n_sam_per_datapoint
        sample_sz = (mu.size()[0] * L,) + mu.size()[1:]
        if L > 1:
            sigma = sigma.repeat(L, 1, 1)
            mu = mu.repeat(L, 1, 1)

        # epsilon is the randomness injected here
        epsilon = mu.new_empty(sample_sz).normal_()
        samples = sigma * epsilon + mu 
        # Cache mu and sigma for objective function later 
        self.mu, self.sigma = mu, sigma

        return samples

class SGVB(nn.Module):
    def __init__(self, bottleneck):
        super(SGVB, self).__init__()
        self.bottleneck = bottleneck
        self.combine = LCCombine('LCCombine')

    def set_geometry(self, beg_rf, end_rf):
        self.combine.set_geometry(beg_rf, end_rf)

    def forward(self, log_pred, target_wav):
        '''
        Compute SGVB estimator from equation 8 in
        https://arxiv.org/pdf/1312.6114.pdf
        Uses formulas from "Autoencoding Variational Bayes",
        Appendix B, "Solution of -D_KL(q_phi(z) || p_theta(z)), Gaussian Case"
        '''
        # B, T, Q, L: n_batch, n_timesteps, n_quant, n_samples_per_datapoint
        # K: n_bottleneck_channels
        # log_pred: (L * B, T, Q), the companded, quantized waveforms.
        # target_wav: (B, T)
        # mu, sigma: (B, T, K), the vectors output by the bottleneck
        # Output: scalar, L(theta, phi, x)
        sigma = self.bottleneck.sigma
        mu = self.bottleneck.mu
        sigma_sq = sigma * sigma
        mu_sq = mu * mu

        # neg_kl_div_gaussian: (B, K)
        channel_dim = 1
        channel_terms = 1.0 + torch.log(sigma_sq) - mu_sq - sigma_sq
        neg_kl_div_gaussian = \
                0.5 * torch.sum(channel_terms, dim=channel_dim, keepdim=True)
        combined_kl = self.combine(neg_kl_div_gaussian)

        L = self.bottleneck.n_sam_per_datapoint
        BL = log_pred.size(0)
        assert BL % L == 0 

        # Note that, unlike torch.nn.CrossEntropy loss, the second term in
        # equation 7 is a function only of the probabilities assigned to the
        # target quantum, if I'm interpreting the formula correctly.  For
        # example, if the output probabilities are out_prob = [0.1, 0.5, 0.1,
        # 0.3] and the target = 1, then only the out_prob[target] value affects
        # the SGVB second term, not out_prob[i != target].
        
        # A second question is, since the following:  In this particular design,
        # the encoder produces only one encoding vector every 320 time steps, while
        # the decoder produces one output prediction every time step, in auto-regressive
        # fashion.  So, for a VAE SGVB training objective, it is not clear how to
        # treat the sum-over-i in equation 8.

        # Here, I've decided to duplicate the gaussian by the upsampling factor. 
        tsz = target_wav.size()
        target_wav_aug = target_wav.repeat(L, 1)
        log_pred_target = torch.gather(log_pred, 1, target_wav_aug.unsqueeze(1))
        log_pred_target_avg = torch.mean(log_pred_target, dim=1)
        sgvb = combined_kl + log_pred_target_avg  
        minibatch_sgvb = torch.mean(sgvb)
        return minibatch_sgvb


class AE(nn.Module):
    def __init__(self, n_in, n_out, bias=True):
        super(AE, self).__init__()
        self.linear = nn.Conv1d(n_in, n_out, 1, bias=bias)

    def forward(self, x):
        out = self.linear(x)
        return out

