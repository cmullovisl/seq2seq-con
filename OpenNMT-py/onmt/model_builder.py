"""
This file is for models creation, which consults options
and creates each encoder and decoder accordingly.
"""
import re
import torch
import torch.nn as nn
from torch.nn.init import xavier_uniform_

import onmt.inputters as inputters
import onmt.modules
from onmt.encoders import str2enc
from torchtext.vocab import Vocab

from onmt.decoders import str2dec

from onmt.modules import Embeddings, VecEmbedding, CopyGenerator
from onmt.modules.util_class import Cast
from onmt.utils.misc import use_gpu
from onmt.utils.logging import logger
from onmt.utils.parse import ArgumentParser

from onmt_vocab_utils.util import vec_to_vocab


def build_embeddings(opt, text_field, for_encoder=True):
    """
    Args:
        opt: the option in current environment.
        text_field(TextMultiField): word and feats field.
        for_encoder(bool): build Embeddings for encoder or decoder?
    """
    emb_dim = opt.src_word_vec_size if for_encoder else opt.tgt_word_vec_size

    if opt.model_type == "vec" and for_encoder:
        return VecEmbedding(
            opt.feat_vec_size,
            emb_dim,
            position_encoding=opt.position_encoding,
            dropout=(opt.dropout[0] if type(opt.dropout) is list
                     else opt.dropout),
        )

    if opt.use_feat_emb:
        num_embs = [len(f.vocab) for _, f in text_field]
        pad_indices = [f.vocab.stoi[f.pad_token] for _, f in text_field]
    else:
        num_embs = [len(f.vocab) for _, f in text_field][:1]
        pad_indices = [f.vocab.stoi[f.pad_token] for _, f in text_field][:1]

    num_word_embeddings, num_feat_embeddings = num_embs[0], num_embs[1:]  
    word_padding_idx, feat_pad_indices = pad_indices[0], pad_indices[1:]  

    fix_word_vecs = opt.fix_word_vecs_enc if for_encoder \
        else opt.fix_word_vecs_dec
    
    conmt = False
    out_vec_size = None
    if ("continuous" in opt.generator_function) and not for_encoder:
        out_vec_size = text_field.base_field.vocab.vectors.size(1)
        conmt = True

    emb = Embeddings(
        word_vec_size=emb_dim,
        position_encoding=opt.position_encoding,
        feat_merge=opt.feat_merge,
        feat_vec_exponent=opt.feat_vec_exponent,
        feat_vec_size=opt.feat_vec_size,
        dropout=opt.dropout[0] if type(opt.dropout) is list else opt.dropout,
        word_padding_idx=word_padding_idx,
        feat_padding_idx=feat_pad_indices,
        word_vocab_size=num_word_embeddings,
        feat_vocab_sizes=num_feat_embeddings,
        sparse=opt.optim == "sparseadam",
        fix_word_vecs=fix_word_vecs,
        tie_embeddings=opt.share_decoder_embeddings and conmt,
        out_vec_size=out_vec_size
    )
    return emb


def build_encoder(opt, embeddings):
    """
    Various encoder dispatcher function.
    Args:
        opt: the option in current environment.
        embeddings (Embeddings): vocab embeddings for this encoder.
    """
    enc_type = opt.encoder_type if opt.model_type == "text" \
        or opt.model_type == "vec" else opt.model_type
    return str2enc[enc_type].from_opt(opt, embeddings)


def build_decoder(opt, embeddings):
    """
    Various decoder dispatcher function.
    Args:
        opt: the option in current environment.
        embeddings (Embeddings): vocab embeddings for this decoder.
    """
    dec_type = "ifrnn" if opt.decoder_type == "rnn" and opt.input_feed \
               else opt.decoder_type
    return str2dec[dec_type].from_opt(opt, embeddings)


def build_generator(model, opt, fields, output_vec_dim=-1):
    # Build Generator.
    if not opt.copy_attn:
        if opt.generator_function == 'continuous-linear':
            generator_modules = [nn.Linear(opt.dec_rnn_size, output_vec_dim)]
            if opt.generator_layer_norm:
                generator_modules.append(nn.LayerNorm(output_vec_dim, eps=1e-6))
            generator = nn.Sequential(*generator_modules)
        elif opt.generator_function == 'continuous-nonlinear': #add a non-linear layer before generating the continuous vector
            generator_modules = [nn.Linear(opt.dec_rnn_size, output_vec_dim), 
                                    nn.ReLU(), 
                                    nn.Linear(output_vec_dim, output_vec_dim)]
            if opt.generator_layer_norm:
                generator_modules.append(nn.LayerNorm(output_vec_dim, eps=1e-6))
            generator = nn.Sequential(*generator_modules)
        else:
            if opt.generator_function == "sparsemax":
                gen_func = onmt.modules.sparse_activations.LogSparsemax(dim=-1)
            else:
                gen_func = nn.LogSoftmax(dim=-1)
            generator = nn.Sequential(
                nn.Linear(opt.dec_rnn_size,
                        len(fields["tgt"].base_field.vocab), bias=not opt.no_generator_bias),
                Cast(torch.float32),
                gen_func
            )
            if opt.share_decoder_embeddings:
                generator[0].weight = model.decoder.embeddings.word_lut.weight
    else:
        tgt_base_field = fields["tgt"].base_field
        vocab_size = len(tgt_base_field.vocab)
        pad_idx = tgt_base_field.vocab.stoi[tgt_base_field.pad_token]
        generator = CopyGenerator(opt.dec_rnn_size, vocab_size, pad_idx)
    
    mtl_generator = None
    if opt.multi_task:
        if len(fields["tgt"].fields) > 1:
            secondary_task_vocab = len(fields["tgt"].fields[1][1].vocab)
            mtl_generator = nn.Sequential(nn.Linear(opt.dec_rnn_size, secondary_task_vocab),
                                nn.LogSoftmax(dim=-1))
        else:
            logger.info("multitask is set but data doesn't contain multitask labels. Ignoring")

    return generator, mtl_generator


# def compare_vocab(v1, v2):
#     v1 = dict(v1)
#     v2 = dict(v2)
#     v1src = v1['src']
#     v1tgt = v1['tgt']

#     v2src = v2['src']
#     v2tgt = v2['tgt']

#     print(v1src)
#     print(v1tgt)
#     input()

def load_vocab(vocab, checkpoint, opt, model_opt, vocab_old=None):
    tgt_vecs = vocab['tgt'].base_field.vocab.vectors
    src_vecs = vocab['src'].base_field.vocab.vectors
    embedding_key = '{}.embeddings.make_embedding.emb_luts.0.0.weight'
    if model_opt.share_decoder_embeddings:
        checkpoint['model'][embedding_key.format('decoder')] = tgt_vecs

    if src_vecs is not None:
        checkpoint['model'][embedding_key.format('encoder')] = src_vecs

    if "continuous" not in model_opt.generator_function:
        checkpoint['generator']['0.weight'] = tgt_vecs

        if not model_opt.no_generator_bias:
            # handle bias: 3 variants
            old_bias = checkpoint['generator']['0.bias']
            # variant 1: zero-bias except for specials
            #N_SPECIALS = 4
            #special_bias = old_bias[:N_SPECIALS]
            #new_bias = torch.zeros(len(tgt_vecs))
            #new_bias[:N_SPECIALS] += special_bias
            #checkpoint['generator']['0.bias'] = new_bias

            # variant 2: delete bias
            #del checkpoint['generator']['0.bias']

            # variant 3: isolate bias relevant to current language
            lang_prefix = opt.langcode + '@'
            bias_idxs  = [i for i, s in enumerate(vocab_old['tgt'].base_field.vocab.itos) if s.startswith(lang_prefix) or '@' not in s]
            #bias_idxs  = [i for i, s in enumerate(vocab_old['tgt'].base_field.vocab.itos) if s.startswith(lang_prefix)]
            #bias_idxs = [0,1,2,3] + bias_idxs
            new_bias = old_bias[bias_idxs]
            checkpoint['generator']['0.bias'] = new_bias

    if model_opt.share_embeddings:
        model_opt.share_embeddings = False

def load_test_model(opt, model_path=None):
    if model_path is None:
        model_path = opt.models[0]
    checkpoint = torch.load(model_path,
                            map_location=lambda storage, loc: storage)

    model_opt = ArgumentParser.ckpt_model_opts(checkpoint['opt'])
    ArgumentParser.update_model_opts(model_opt)
    ArgumentParser.validate_model_opts(model_opt)

    if opt.use_lang is not None:
        vocab = checkpoint['vocab']
        tgt_vocab = vocab['tgt'].base_field.vocab
        counter = tgt_vocab.freqs

        embedding_key = '{}.embeddings.make_embedding.emb_luts.0.0.weight'
        tgt_vectors = checkpoint['model'][embedding_key.format('decoder')]

        lang_prefix = opt.use_lang + '@'
        new_ctr  = {s: freq for s, freq in counter.items() if s.startswith(lang_prefix)}
        specials = {s: freq for s, freq in counter.items() if '@' not in s}
        new_ctr.update(specials)

        new_stoi = {s: tgt_vocab.stoi[s] for s in new_ctr}
        new_vocab = Vocab(new_ctr, specials=tgt_vocab.itos[:4])
        new_vocab.set_vectors(new_stoi, tgt_vectors, dim=tgt_vectors.size(1))

        vocab['tgt'].base_field.vocab = new_vocab

        load_vocab(vocab, checkpoint, opt, model_opt)

    if opt.new_vocab is not None:
        vocab_old = checkpoint['vocab']
        vocab = torch.load(opt.new_vocab)
        load_vocab(vocab, checkpoint, opt, model_opt, vocab_old=vocab_old)

    elif opt.tgt_embeddings or opt.src_embeddings:
        # XXX what about non-shared decoder embeddings?
        vocab = checkpoint['vocab']

        def _set_vocab_from_embeddings(side, embeddings, vocab):
            # extract specials vectors from encoder/decoder, create specials
            # vocab, create vocab from .vec file
            specials_vocab = vocab[side].base_field.vocab
            embedding_key = '{}.embeddings.make_embedding.emb_luts.0.0.weight'.format(
                    'encoder' if side == 'src' else 'decoder')
            special_vectors = checkpoint['model'][embedding_key]
            specials_vocab.vectors = special_vectors
            new_vocab = vec_to_vocab(embeddings, specials_vocab)
            vocab[side].base_field.vocab = new_vocab

        if opt.src_embeddings:
            _set_vocab_from_embeddings('src', opt.src_embeddings, vocab)
        if opt.tgt_embeddings:
            _set_vocab_from_embeddings('tgt', opt.tgt_embeddings, vocab)

        load_vocab(vocab, checkpoint, opt, model_opt)

    else:
        vocab = checkpoint['vocab']
    if inputters.old_style_vocab(vocab):
        fields = inputters.load_old_vocab(
            vocab, opt.data_type, dynamic_dict=model_opt.copy_attn
        )
    else:
        fields = vocab

    model = build_base_model(model_opt, fields, use_gpu(opt), checkpoint,
                             opt.gpu)
    if opt.fp32:
        model.float()
    model.eval()
    model.generator.eval()
    return fields, model, model_opt


def build_base_model(model_opt, fields, gpu, checkpoint=None, gpu_id=None):
    """Build a model from opts.

    Args:
        model_opt: the option loaded from checkpoint. It's important that
            the opts have been updated and validated. See
            :class:`onmt.utils.parse.ArgumentParser`.
        fields (dict[str, torchtext.data.Field]):
            `Field` objects for the model.
        gpu (bool): whether to use gpu.
        checkpoint: the model gnerated by train phase, or a resumed snapshot
                    model from a stopped training.
        gpu_id (int or NoneType): Which GPU to use.

    Returns:
        the NMTModel.
    """

    # for back compat when attention_dropout was not defined
    try:
        model_opt.attention_dropout
    except AttributeError:
        model_opt.attention_dropout = model_opt.dropout

    # Build embeddings.
    if model_opt.model_type == "text" or model_opt.model_type == "vec":
        src_field = fields["src"]
        src_field.base_field.vocab.stoi.default_factory = lambda: src_field.base_field.vocab.unk_index
        src_emb = build_embeddings(model_opt, src_field)
    else:
        src_emb = None

    # Build encoder.
    encoder = build_encoder(model_opt, src_emb)

    # Build decoder.
    tgt_field = fields["tgt"]
    tgt_field.base_field.vocab.stoi.default_factory = lambda: tgt_field.base_field.vocab.unk_index
    tgt_emb = build_embeddings(model_opt, tgt_field, for_encoder=False)

    # Share the embedding matrix - preprocess with share_vocab required.
    if model_opt.share_embeddings:
        # src/tgt vocab should be the same if `-share_vocab` is specified.
        # assert src_field.base_field.vocab == tgt_field.base_field.vocab, \
        #     "preprocess with -share_vocab if you use share_embeddings"

        tgt_emb.word_lut.weight = src_emb.word_lut.weight
        src_emb.word_lut.weight.requires_grad = False

    decoder = build_decoder(model_opt, tgt_emb)

    output_vec_dim = -1
    if "continuous" in model_opt.generator_function:
        #make target embeddings
        if model_opt.share_decoder_embeddings and model_opt.pre_word_vecs_dec:
            # TODO properly implement
            tgt_out_vectors = src_emb.word_lut.weight
            assert(False)
        else:
            tgt_out_vectors = tgt_field.base_field.vocab.vectors
        if model_opt.center:
            center_emb = tgt_out_vectors.sum(dim=0, keepdim=True) / (tgt_out_vectors.size(0))
            tgt_out_vectors = tgt_out_vectors - center_emb
        tgt_out_vectors_unitnorm = nn.functional.normalize(tgt_out_vectors, p=2, dim=1)

        tgt_out_emb = nn.Embedding(tgt_out_vectors.size(0), tgt_out_vectors.size(1))
        tgt_out_emb.weight.data.copy_(tgt_out_vectors_unitnorm)
        tgt_out_emb.weight.requires_grad = False # do not train the embeddings
        output_vec_dim = tgt_out_vectors.size(1)

    # Build NMTModel(= encoder + decoder).
    if gpu and gpu_id is not None:
        device = torch.device("cuda", gpu_id)
    elif gpu and not gpu_id:
        device = torch.device("cuda")
    elif not gpu:
        device = torch.device("cpu")
    model = onmt.models.NMTModel(encoder, decoder)

    # Generator
    generator, mtl_generator = build_generator(model, model_opt, fields, output_vec_dim=output_vec_dim)

    # Load the model states from checkpoint or initialize them.
    if checkpoint is not None:
        # This preserves backward-compat for models using customed layernorm
        def fix_key(s):
            s = re.sub(r'(.*)\.layer_norm((_\d+)?)\.b_2',
                       r'\1.layer_norm\2.bias', s)
            s = re.sub(r'(.*)\.layer_norm((_\d+)?)\.a_2',
                       r'\1.layer_norm\2.weight', s)
            return s

        checkpoint['model'] = {fix_key(k): v
                               for k, v in checkpoint['model'].items()}
        # end of patch for backward compatibility

        model.load_state_dict(checkpoint['model'], strict=False)
        generator.load_state_dict(checkpoint['generator'], strict=False)
        if mtl_generator is not None and 'mtl_generator' in checkpoint:  # the second argument is when one loads a nonmultitask model and trains a multitask predictor on it
            mtl_generator.load_state_dict(checkpoint['mtl_generator'], strict=False)
        elif mtl_generator is not None:
            if model_opt.param_init != 0.0:
                for p in mtl_generator.parameters():
                    p.data.uniform_(-model_opt.param_init, model_opt.param_init)
                for p in mtl_generator.parameters():
                    p.data.uniform_(-model_opt.param_init, model_opt.param_init)
            if model_opt.param_init_glorot:
                for p in mtl_generator.parameters():
                    if p.dim() > 1:
                        xavier_uniform_(p)
                for p in mtl_generator.parameters():
                    if p.dim() > 1:
                        xavier_uniform_(p)
    else:
        if model_opt.param_init != 0.0:
            for p in model.parameters():
                p.data.uniform_(-model_opt.param_init, model_opt.param_init)
            for p in generator.parameters():
                p.data.uniform_(-model_opt.param_init, model_opt.param_init)
        if model_opt.param_init_glorot:
            for p in model.parameters():
                if p.dim() > 1:
                    xavier_uniform_(p)
            for p in generator.parameters():
                if p.dim() > 1:
                    xavier_uniform_(p)

        if hasattr(model.encoder, 'embeddings'):
            model.encoder.embeddings.load_pretrained_vectors(
                model_opt.pre_word_vecs_enc)
        if hasattr(model.decoder, 'embeddings'):
            model.decoder.embeddings.load_pretrained_vectors(
                model_opt.pre_word_vecs_dec)

    model.generator = generator
    model.mtl_generator = mtl_generator
    if "continuous" in model_opt.generator_function:
        model.decoder.tgt_out_emb = tgt_out_emb

    model.to(device)
    if model_opt.model_dtype == 'fp16' and model_opt.optim == 'fusedadam':
        model.half()

    if ("continuous" in model_opt.generator_function
            and model_opt.share_decoder_embeddings):
        model.decoder.embeddings.tie_embeddings(tgt_out_emb.weight,
            model_opt.sync_output_embeddings)

    if model_opt.detached_embeddings:
        for field in [src_field, tgt_field]:
            for _, f in field:
                if f.use_vocab:
                    f.vocab.vectors = None

    return model


def build_model(model_opt, opt, fields, checkpoint):
    logger.info('Building model...')
    model = build_base_model(model_opt, fields, use_gpu(opt), checkpoint)
    logger.info(model)
    return model
