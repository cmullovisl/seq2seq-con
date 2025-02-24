import os
import torch

from collections import deque
from onmt.utils.logging import logger

from copy import deepcopy


def build_model_saver(model_opt, opt, model, fields, optim):
    model_saver = ModelSaver(opt.save_model,
                             model,
                             model_opt,
                             fields,
                             optim,
                             opt.keep_checkpoint,
                             opt.train_only_sec_task)
    return model_saver


class ModelSaverBase(object):
    """Base class for model saving operations

    Inherited classes must implement private methods:
    * `_save`
    * `_rm_checkpoint
    """

    def __init__(self, base_path, model, model_opt, fields, optim,
                 keep_checkpoint=-1, only_sec=False):
        self.base_path = base_path
        self.model = model
        self.model_opt = model_opt
        self.fields = fields
        self.optim = optim
        self.last_saved_step = None
        self.keep_checkpoint = keep_checkpoint
        if keep_checkpoint > 0:
            self.checkpoint_queue = deque([], maxlen=keep_checkpoint)
        self.only_sec = only_sec

    def save(self, step, moving_average=None):
        """Main entry point for model saver

        It wraps the `_save` method with checks and apply `keep_checkpoint`
        related logic
        """

        if self.keep_checkpoint == 0 or step == self.last_saved_step:
            return

        save_model = self.model
        if moving_average:
            model_params_data = []
            for avg, param in zip(moving_average, save_model.parameters()):
                model_params_data.append(param.data)
                param.data = avg.data

        chkpt, chkpt_name = self._save(step, save_model)
        self.last_saved_step = step

        if moving_average:
            for param_data, param in zip(model_params_data,
                                         save_model.parameters()):
                param.data = param_data

        if self.keep_checkpoint > 0:
            if len(self.checkpoint_queue) == self.checkpoint_queue.maxlen:
                todel = self.checkpoint_queue.popleft()
                self._rm_checkpoint(todel)
            self.checkpoint_queue.append(chkpt_name)

    def _save(self, step):
        """Save a resumable checkpoint.

        Args:
            step (int): step number

        Returns:
            (object, str):

            * checkpoint: the saved object
            * checkpoint_name: name (or path) of the saved checkpoint
        """

        raise NotImplementedError()

    def _rm_checkpoint(self, name):
        """Remove a checkpoint

        Args:
            name(str): name that indentifies the checkpoint
                (it may be a filepath)
        """

        raise NotImplementedError()


class ModelSaver(ModelSaverBase):
    """Simple model saver to filesystem"""

    def _save(self, step, model):
        model_state_dict = model.state_dict()
        model_state_dict = {k: v for k, v in model_state_dict.items()
                            if 'generator' not in k}
        generator_state_dict = model.generator.state_dict()
        optim_state_dict = self.optim.state_dict(),

        if self.model_opt.detached_embeddings:
            embedding_key = '{}.embeddings.make_embedding.emb_luts.0.0.weight'
            # FIXME do properly
            tgt_special_vecs = model_state_dict[embedding_key.format('decoder')][:32].clone()
            model_state_dict[embedding_key.format('decoder')] = tgt_special_vecs

            if self.model_opt.share_decoder_embeddings:
                model_state_dict[embedding_key.format('encoder')] = tgt_special_vecs
            else:
                src_special_vecs = model_state_dict[embedding_key.format('encoder')][:32].clone()
                model_state_dict[embedding_key.format('encoder')] = src_special_vecs

            if 'continuous' in self.model_opt.generator_function:
                model_state_dict['decoder.tgt_out_emb.weight'] = tgt_special_vecs
            elif self.model_opt.share_decoder_embeddings:
                generator_state_dict['weight.0'] = tgt_special_vecs

        if self.model_opt.reset_optim == 'all':
            optim_state_dict = None
        elif self.model_opt.reset_optim == 'states':
            del optim_state_dict['optimizer']

        mtl_generator_state_dict = None
        if model.mtl_generator is not None:
            mtl_generator_state_dict = model.mtl_generator.state_dict()

        # NOTE: We need to trim the vocab to remove any unk tokens that
        # were not originally here.

        vocab = deepcopy(self.fields)
        for side in ["src", "tgt"]:
            keys_to_pop = []
            if hasattr(vocab[side], "fields"):
                unk_token = vocab[side].fields[0][1].vocab.itos[0]
                for key, value in vocab[side].fields[0][1].vocab.stoi.items():
                    if value == 0 and key != unk_token:
                        keys_to_pop.append(key)
                for key in keys_to_pop:
                    vocab[side].fields[0][1].vocab.stoi.pop(key, None)

        checkpoint = {
            'model': model_state_dict,
            'generator': generator_state_dict,
            'mtl_generator': mtl_generator_state_dict,
            'vocab': vocab,
            'opt': self.model_opt,
            'optim': optim_state_dict,
        }

        if self.only_sec:
            logger.info("Saving checkpoint %s_sec_step_%d.pt" % (self.base_path, step))
            checkpoint_path = '%s_sec_step_%d.pt' % (self.base_path, step)
        else:
            logger.info("Saving checkpoint %s_step_%d.pt" % (self.base_path, step))
            checkpoint_path = '%s_step_%d.pt' % (self.base_path, step)
        torch.save(checkpoint, checkpoint_path)
        return checkpoint, checkpoint_path

    def _rm_checkpoint(self, name):
        os.remove(name)
