import torch
import torch.nn as nn
import random
from core.models.st_gcn.st_gcn_aaai18 import st_gcn_baseline

from .positional_encoding import PositionalEncoding
from .rpr import TransformerEncoderRPR, TransformerEncoderLayerRPR, TransformerDecoderLayerRPR, TransformerDecoderRPR
from torch import Tensor
from typing import Optional

def get_pad_mask(seq: Tensor, pad_idx: int) -> Tensor:
    return seq == pad_idx  

class MusicTransformer(nn.Module):

    def __init__(
            self,
            vocab_size: int,
            pose_net: nn.Module,
            num_heads=8,
            d_model=512,
            dim_feedforward=1024,
            dropout=0.1,
            encoder_max_seq=300,
            decoder_max_seq=512,
            rpr=False,
            num_encoder_layers=0,
            num_decoder_layers=6,
            control_dim=12,
            use_control=False,
    ):
        super(MusicTransformer, self).__init__()

        self.num_encoder_layers = num_encoder_layers
        self.num_decoder_layers = num_decoder_layers
        self.nhead = num_heads
        self.d_model = d_model
        self.d_ff = dim_feedforward
        self.dropout = dropout
        self.encoder_max_seq = encoder_max_seq
        self.decoder_max_seq = decoder_max_seq
        self.rpr = rpr
        self.vocab_size = vocab_size
        self.control_dim = control_dim
        self.concat_dim = d_model + 1 + control_dim
        self.use_control = use_control

        # Todo rel pos embedding
        self.embedding = nn.Embedding(vocab_size, self.d_model)
        # self.flow_rgb_embedding = nn.Linear(1024, self.d_model)

        if self.use_control:
            self.concat_fc = nn.Sequential(
                nn.Linear(self.concat_dim, self.d_model),
                nn.LeakyReLU(negative_slope=0.1, inplace=True)
            )
            self.control_positional_encoding = PositionalEncoding(
                control_dim,
                dropout=self.dropout,
                max_len=self.decoder_max_seq
            )
        self.pose_net = pose_net

        # Positional encoding
        self.positional_encoding = PositionalEncoding(self.d_model, self.dropout, self.decoder_max_seq)

        # Base transformer
        if (not self.rpr):
            self.transformer = nn.Transformer(
                d_model=self.d_model, nhead=self.nhead, num_encoder_layers=self.num_encoder_layers,
                num_decoder_layers=self.num_decoder_layers, dropout=self.dropout,  # activation=self.ff_activ,
                dim_feedforward=self.d_ff,
            )
        # RPR Transformer
        else:
            encoder_norm = nn.LayerNorm(self.d_model)
            encoder_layer = TransformerEncoderLayerRPR(self.d_model, self.nhead, self.d_ff, self.dropout,
                                                        er_len=self.encoder_max_seq)
            encoder = TransformerEncoderRPR(encoder_layer, self.num_encoder_layers, encoder_norm)
            decoder_layer = TransformerDecoderLayerRPR(
                self.d_model, self.nhead, self.d_ff, self.dropout, er_len=self.decoder_max_seq
            )
            decoder_norm = nn.LayerNorm(self.d_model)
            decoder = TransformerDecoderRPR(
                decoder_layer, self.num_decoder_layers, norm=decoder_norm
            )
            self.transformer = nn.Transformer(
                d_model=self.d_model, nhead=self.nhead, num_encoder_layers=self.num_encoder_layers,
                num_decoder_layers=self.num_decoder_layers, dropout=self.dropout,  # activation=self.ff_activ,
                dim_feedforward=self.d_ff, custom_decoder=decoder, custom_encoder=encoder
            )

        # Final output is a softmaxed linear layer
        self.Wout = nn.Linear(self.d_model, vocab_size)
        self.softmax = nn.Softmax(dim=-1)


    def forward(
            self,
            pose: Tensor,
            tgt: Tensor,
            use_mask=True,
            pad_idx=242,
            control: Optional[Tensor] = None
    ):

        tgt, subsequent_mask, tgt_key_padding_mask = self.get_tgt_embedding(tgt, pad_idx=pad_idx, use_mask=use_mask)

        if self.use_control:
            tgt = self.forward_concat_fc(tgt, control=control)

        pose = self.forward_pose_net(pose)

        x_out = self.transformer(
            src=pose,
            tgt=tgt,
            tgt_mask=subsequent_mask,
            tgt_key_padding_mask=tgt_key_padding_mask
        )

        # Back to (batch_size, max_seq, d_model)
        y = self.get_output(x_out)
        # y = self.softmax(y)

        return y

    def forward_pose_net(self, pose: Tensor):
        # Todo flow
        
        pose = self.pose_net(pose)
        pose = pose.permute(2, 0, 1)  # [B_0, C_1, T_2] -> [T_2, B_0, C_1]
  
        return pose

    def get_tgt_embedding(self, tgt, pad_idx=-1, use_mask=True):
        subsequent_mask, tgt_key_padding_mask = self.get_masks(
            tgt, pad_idx, use_mask=use_mask
        )

        tgt = self.embedding(tgt)
        tgt = tgt.permute(1, 0, 2)
        
        # position embedding
        tgt = self.positional_encoding(tgt)

        return tgt, subsequent_mask, tgt_key_padding_mask

    def get_output(self, x_out: Tensor):
        x_out = x_out.permute(1, 0, 2)

        y = self.Wout(x_out)
        return y

    def get_masks(self, tgt, pad_idx, use_mask=False):
        if use_mask:
            subsequent_mask = self.transformer.generate_square_subsequent_mask(tgt.shape[1]).to(tgt.device)
            tgt_key_padding_mask = get_pad_mask(tgt, pad_idx)
        else:
            subsequent_mask = None
            tgt_key_padding_mask = None

        return subsequent_mask, tgt_key_padding_mask

    def forward_concat_fc(self, tgt: Tensor, control: Optional[Tensor] = None) -> Tensor:
        """

        :param tgt: [T, B, D]
        :param control: [B, T, D]
        :return:
        """
        T, B, _D = tgt.shape
        if control is None:
            default = torch.ones(T, B, 1, device=tgt.device)
            control = torch.zeros(T, B, self.control_dim, device=tgt.device)
        else:
            default = torch.zeros(T, B, 1, device=tgt.device)

            if control.ndim == 1:  # [D], D = 12
                control = control.repeat(T, B, 1)  # [D] -> [T, B, D]
                control[0] = 0.
            else:
                control = control.transpose(0, 1)  # [B, T, D] -> [T, B, D]
                control = control[:T]  

        control = self.control_positional_encoding(control)
        concat = torch.cat([tgt, default, control], dim=-1)  # [T, B, D1 + 1 + D2]
        out = self.concat_fc(concat)  # [T, B, D1 + 1 + D2] -> [T, B, D1]
        return out

        # generate

    def generate(
            self,
            pose: Tensor,
            target_seq_length=1024,
            beam=0,
            beam_chance=1.0,
            pad_idx=0,
            eos_idx=0,
            sos_idx=0,
            use_mask=True,
            control: Optional[Tensor] = None
    ):

        assert (not self.training), "Cannot generate while in training mode"

        print("Generating sequence of max length:", target_seq_length)

        pose = self.forward_pose_net(pose)

        memory: Tensor = self.transformer.encoder(pose)
        if beam > 0:
            memory = memory.repeat(1, beam, 1)
            gen_seq = torch.full((beam, target_seq_length), pad_idx, dtype=torch.long, device=pose.device)
        else:
            gen_seq = torch.full((1, target_seq_length), pad_idx, dtype=torch.long, device=pose.device)
        
        num_primer = 1
        gen_seq[..., :num_primer] = sos_idx
        cur_i = num_primer
        
        while (cur_i < target_seq_length):
            tgt, subsequent_mask, tgt_key_padding_mask = self.get_tgt_embedding(
                gen_seq[..., :cur_i], pad_idx=pad_idx, use_mask=use_mask
            )
            if self.use_control:
                tgt = self.forward_concat_fc(tgt, control=control)
            y = self.transformer.decoder(
                tgt,
                memory,
                tgt_mask=subsequent_mask,
                tgt_key_padding_mask=tgt_key_padding_mask
            )
            y = self.softmax(self.get_output(y))
            token_probs = y[:, cur_i - 1, :]  # [B, T, C] 


            if (beam == 0):
                beam_ran = 2.0
            else:
                beam_ran = random.uniform(0, 1)

            if (beam_ran <= beam_chance):
                token_probs = token_probs.flatten()
                top_res, top_i = torch.topk(token_probs, beam)

                beam_rows = top_i // self.vocab_size
                beam_cols = top_i % self.vocab_size

                # update gen_seq
                gen_seq = gen_seq[beam_rows, :]
                gen_seq[..., cur_i] = beam_cols

            else:
                distrib = torch.distributions.categorical.Categorical(probs=token_probs)
                next_token = distrib.sample()
                # print("next token:",next_token)
                gen_seq[:, cur_i] = next_token

                # Let the transformer decide to end if it wants to
                if (next_token == eos_idx):
                    print("Model called end of sequence at:", cur_i, "/", target_seq_length)
                    break

            cur_i += 1
            if (cur_i % 50 == 0):
                print(cur_i, "/", target_seq_length)
        # beam search
        return gen_seq[:1, :cur_i]


def music_transformer_dev_baseline(
        vocab_size,
        num_heads=8,
        d_model=512,
        dim_feedforward=1024,
        dropout=0.1,
        encoder_max_seq=300,
        decoder_max_seq=512,
        rpr=False,
        num_encoder_layers=6,
        num_decoder_layers=0,
        layout='body25',
        use_control=False,
        layers=10 
):
    in_channels = 2 if layout == 'hands' else 3
    pose_net = st_gcn_baseline(
        in_channels, d_model, layers=layers, layout=layout, dropout=dropout
    )

    music_transformer = MusicTransformer(
        vocab_size,
        pose_net,
        num_heads=num_heads,
        d_model=d_model,
        dim_feedforward=dim_feedforward,
        dropout=dropout,
        encoder_max_seq=encoder_max_seq,
        decoder_max_seq=decoder_max_seq,
        rpr=rpr,
        num_encoder_layers=num_encoder_layers,
        num_decoder_layers=num_decoder_layers,
        use_control=use_control,
    )
    return music_transformer