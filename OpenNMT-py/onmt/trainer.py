"""
    This is the loadable seq2seq trainer library that is
    in charge of training details, loss compute, and statistics.
    See train.py for a use case of this library.

    Note: To make this a general library, we implement *only*
          mechanism things here(i.e. what to do), and leave the strategy
          things to users(i.e. how to do it). Also see train.py(one of the
          users of this library) for the strategy things we do.
"""

import torch
import numpy as np
import traceback
from contextlib import nullcontext

import onmt.utils
from onmt.utils.logging import logger


def build_trainer(opt, device_id, model, fields, optim, model_saver=None):
    """
    Simplify `Trainer` creation based on user `opt`s*

    Args:
        opt (:obj:`Namespace`): user options (usually from argument parsing)
        model (:obj:`onmt.models.NMTModel`): the model to train
        fields (dict): dict of fields
        optim (:obj:`onmt.utils.Optimizer`): optimizer used during training
        data_type (str): string describing the type of data
            e.g. "text", "img", "audio"
        model_saver(:obj:`onmt.models.ModelSaverBase`): the utility object
            used to save the model
    """

    tgt_field = dict(fields)["tgt"].base_field
    train_loss = onmt.utils.loss.build_loss_compute(model, tgt_field, opt)
    valid_loss = onmt.utils.loss.build_loss_compute(
        model, tgt_field, opt, train=False)

    trunc_size = opt.truncated_decoder  # Badly named...
    shard_size = opt.max_generator_batches if opt.model_dtype == 'fp32' else 0
    norm_method = opt.normalization
    accum_count = opt.accum_count
    accum_steps = opt.accum_steps
    n_gpu = opt.world_size
    average_decay = opt.average_decay
    average_every = opt.average_every
    dropout = opt.dropout
    dropout_steps = opt.dropout_steps
    if device_id >= 0:
        gpu_rank = opt.gpu_ranks[device_id]
    else:
        gpu_rank = 0
        n_gpu = 0
    gpu_verbose_level = opt.gpu_verbose_level

    earlystopper = onmt.utils.EarlyStopping(
        opt.early_stopping, scorers=onmt.utils.scorers_from_opts(opt)) \
        if opt.early_stopping > 0 else None

    report_manager = onmt.utils.build_report_manager(opt, gpu_rank)
    trainer = onmt.Trainer(model, train_loss, valid_loss, optim, trunc_size,
                           shard_size, norm_method,
                           accum_count, accum_steps,
                           n_gpu, gpu_rank,
                           gpu_verbose_level, report_manager,
                           with_align=True if opt.lambda_align > 0 else False,
                           model_saver=model_saver if gpu_rank == 0 else None,
                           average_decay=average_decay,
                           average_every=average_every,
                           model_dtype=opt.model_dtype,
                           earlystopper=earlystopper,
                           dropout=dropout,
                           dropout_steps=dropout_steps,
                           use_feat_emb=opt.use_feat_emb,
                           denoise=opt.denoise)
    return trainer


class Trainer(object):
    """
    Class that controls the training process.

    Args:
            model(:py:class:`onmt.models.model.NMTModel`): translation model
                to train
            train_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            valid_loss(:obj:`onmt.utils.loss.LossComputeBase`):
               training loss computation
            optim(:obj:`onmt.utils.optimizers.Optimizer`):
               the optimizer responsible for update
            trunc_size(int): length of truncated back propagation through time
            shard_size(int): compute loss in shards of this size for efficiency
            data_type(string): type of the source input: [text|img|audio]
            norm_method(string): normalization methods: [sents|tokens]
            accum_count(list): accumulate gradients this many times.
            accum_steps(list): steps for accum gradients changes.
            report_manager(:obj:`onmt.utils.ReportMgrBase`):
                the object that creates reports, or None
            model_saver(:obj:`onmt.models.ModelSaverBase`): the saver is
                used to save a checkpoint.
                Thus nothing will be saved if this parameter is None
    """

    def __init__(self, model, train_loss, valid_loss, optim,
                 trunc_size=0, shard_size=32,
                 norm_method="sents", accum_count=[1],
                 accum_steps=[0],
                 n_gpu=1, gpu_rank=1, gpu_verbose_level=0,
                 report_manager=None, with_align=False, model_saver=None,
                 average_decay=0, average_every=1, model_dtype='fp32',
                 earlystopper=None, dropout=[0.3], dropout_steps=[0],
                 use_feat_emb=False, denoise=False):
        # Basic attributes.
        self.model = model
        self.train_loss = train_loss
        self.valid_loss = valid_loss
        self.optim = optim
        self.trunc_size = trunc_size
        self.shard_size = shard_size
        self.norm_method = norm_method
        self.accum_count_l = accum_count
        self.accum_count = accum_count[0]
        self.accum_steps = accum_steps
        self.n_gpu = n_gpu
        self.gpu_rank = gpu_rank
        self.gpu_verbose_level = gpu_verbose_level
        self.report_manager = report_manager
        self.with_align = with_align
        self.model_saver = model_saver
        self.average_decay = average_decay
        self.moving_average = None
        self.average_every = average_every
        self.model_dtype = model_dtype
        self.earlystopper = earlystopper
        self.dropout = dropout
        self.dropout_steps = dropout_steps
        self.use_feat_emb = use_feat_emb
        self.denoise = denoise
        self.params_word_shuffle = 3
        self.params_word_dropout = 0

        for i in range(len(self.accum_count_l)):
            assert self.accum_count_l[i] > 0
            if self.accum_count_l[i] > 1:
                assert self.trunc_size == 0, \
                    """To enable accumulated gradients,
                       you must disable target sequence truncating."""

        # Set model in training mode.
        self.model.train()

    def _accum_count(self, step):
        for i in range(len(self.accum_steps)):
            if step > self.accum_steps[i]:
                _accum = self.accum_count_l[i]
        return _accum

    def _maybe_update_dropout(self, step):
        for i in range(len(self.dropout_steps)):
            if step > 1 and step == self.dropout_steps[i] + 1:
                self.model.update_dropout(self.dropout[i])
                logger.info("Updated dropout to %f from step %d"
                            % (self.dropout[i], step))

    def _accum_batches(self, iterator):
        batches = []
        normalization = 0
        self.accum_count = self._accum_count(self.optim.training_step)
        for batch in iterator:
            batches.append(batch)
            if self.norm_method == "tokens":
                num_tokens = batch.tgt[1:, :, 0].ne(
                    self.train_loss.padding_idx).sum()
                normalization += num_tokens.item()
            else:
                normalization += batch.batch_size
            if len(batches) == self.accum_count:
                yield batches, normalization
                self.accum_count = self._accum_count(self.optim.training_step)
                batches = []
                normalization = 0
        if batches:
            yield batches, normalization

    def _update_average(self, step):
        if self.moving_average is None:
            copy_params = [params.detach().float()
                           for params in self.model.parameters()]
            self.moving_average = copy_params
        else:
            average_decay = max(self.average_decay,
                                1 - (step + 1)/(step + 10))
            for (i, avg), cpt in zip(enumerate(self.moving_average),
                                     self.model.parameters()):
                self.moving_average[i] = \
                    (1 - average_decay) * avg + \
                    cpt.detach().float() * average_decay

    def train(self,
              train_iter,
              train_steps,
              save_checkpoint_steps=5000,
              valid_iter=None,
              valid_steps=10000):
        """
        The main training loop by iterating over `train_iter` and possibly
        running validation on `valid_iter`.

        Args:
            train_iter: A generator that returns the next training batch.
            train_steps: Run training for this many iterations.
            save_checkpoint_steps: Save a checkpoint every this many
              iterations.
            valid_iter: A generator that returns the next validation batch.
            valid_steps: Run evaluation every this many iterations.

        Returns:
            The gathered statistics.
        """
        if valid_iter is None:
            logger.info('Start training loop without validation...')
        else:
            logger.info('Start training loop and validate every %d steps...',
                        valid_steps)

        total_stats = onmt.utils.Statistics()
        report_stats = onmt.utils.Statistics()
        self._start_report_manager(start_time=total_stats.start_time)

        for i, (batches, normalization) in enumerate(
                self._accum_batches(train_iter)):
            step = self.optim.training_step
            # UPDATE DROPOUT
            self._maybe_update_dropout(step)

            if self.gpu_verbose_level > 1:
                logger.info("GpuRank %d: index: %d", self.gpu_rank, i)
            if self.gpu_verbose_level > 0:
                logger.info("GpuRank %d: reduce_counter: %d \
                            n_minibatch %d"
                            % (self.gpu_rank, i + 1, len(batches)))

            if self.n_gpu > 1:
                normalization = sum(onmt.utils.distributed
                                    .all_gather_list
                                    (normalization))

            self._gradient_accumulation(
                batches, normalization, total_stats,
                report_stats)

            if self.average_decay > 0 and i % self.average_every == 0:
                self._update_average(step)

            report_stats = self._maybe_report_training(
                step, train_steps,
                self.optim.learning_rate(),
                report_stats)

            if valid_iter is not None and step % valid_steps == 0:
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: validate step %d'
                                % (self.gpu_rank, step))
                valid_stats = self.validate(
                    valid_iter, moving_average=self.moving_average)
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: gather valid stat \
                                step %d' % (self.gpu_rank, step))
                valid_stats = self._maybe_gather_stats(valid_stats)
                if self.gpu_verbose_level > 0:
                    logger.info('GpuRank %d: report stat step %d'
                                % (self.gpu_rank, step))
                self._report_step(self.optim.learning_rate(),
                                  step, valid_stats=valid_stats)
                # Run patience mechanism
                if self.earlystopper is not None:
                    self.earlystopper(valid_stats, step)
                    # If the patience has reached the limit, stop training
                    if self.earlystopper.has_stopped():
                        break

            if (self.model_saver is not None
                and (save_checkpoint_steps != 0
                     and step % save_checkpoint_steps == 0)):
                self.model_saver.save(step, moving_average=self.moving_average)

            if train_steps > 0 and step >= train_steps:
                break

        if self.model_saver is not None:
            self.model_saver.save(step, moving_average=self.moving_average)
        return total_stats

    def word_shuffle(self, x, l):
        """
        Randomly shuffle input words.
        """
        if self.params_word_shuffle == 0:
            return x, l

        # define noise word scores
        #noise = np.random.uniform(0, self.params_word_shuffle, size=(x.size(0) - 1, x.size(1)))
        noise = self.params_word_shuffle * torch.rand(x.size(0) - 1, x.size(1), device=x.device)
        noise[0] = -1  # do not move start sentence symbol

        assert self.params_word_shuffle > 1
        x2 = x.clone()
        for i in range(l.size(0)):
            # generate a random permutation
            #scores = np.arange(l.cpu()[i] - 1) + noise[:l.cpu()[i] - 1, i]
            scores = torch.arange(l[i] - 1, device=x.device).to(noise.dtype) + noise[:l[i] - 1, i]
            permutation = scores.argsort()
            # shuffle words
            #x2[:l[i] - 1, i].copy_(x2[:l[i] - 1, i][torch.from_numpy(permutation).cuda()])
            x2[:l[i] - 1, i].copy_(x2[:l[i] - 1, i][permutation])
        return x2, l

    def word_dropout(self, x, l):
        """
        Randomly drop input words.
        """
        if self.params_word_dropout == 0:
            return x, l
        assert 0 < self.params_word_dropout < 1

        # define words to drop
        ##eos = self.params.eos_index
        ##bos = self.model.fields['src'].base_field.vocab.stoi['<s>']
        ##eos = self.model.fields['src'].base_field.vocab.stoi['</s>']
        ##bos = 2
        ##eos = 3
        ##assert (x[0] == bos).sum() == l.size(0)
        #keep = np.random.rand(x.size(0) - 1, x.size(1)) >= self.params_word_dropout
        #keep[0] = 1  # do not drop the start sentence symbol

        #sentences = []
        #lengths = []
        #for i in range(l.size(0)):
        #    #assert x[l[i] - 1, i] == eos
        #    words = x[:l[i] - 1, i].tolist()
        #    # randomly drop words from the input
        #    new_s = [w for j, w in enumerate(words) if keep[j, i]]
        #    # we need to have at least one word in the sentence (more than the start / end sentence symbols)
        #    if len(new_s) == 1:
        #        new_s.append(words[np.random.randint(1, len(words))])
        #    #new_s.append(eos)
        #    #assert len(new_s) >= 3 and new_s[0] == bos and new_s[-1] == eos
        #    #assert len(new_s) >= 3 and new_s[-1] == eos
        #    assert len(new_s) >= 2
        #    sentences.append(new_s)
        #    lengths.append(len(new_s))
        ## re-construct input
        #l2 = torch.LongTensor(lengths)
        #x2 = torch.LongTensor(l2.max(), l2.size(0)).fill_(self.train_loss.padding_idx)
        #for i in range(l2.size(0)):
        #    print(torch.LongTensor(sentences[i]).shape)
        #    print(x2[:l2[i], i].shape)
        #    x2[:l2[i], i].copy_(torch.LongTensor(sentences[i]).squeeze())
        #print(x2.shape)
        #print(l2)
        #return x2, l2
        keep = np.random.rand(x.size(0), x.size(1)) >= self.params_word_dropout
        keep[0] = 1  # do not drop the start sentence symbol

        sentences = []
        lengths = []
        for i in range(l.size(0)):
            words = x[:l[i], i].squeeze().tolist()
            # randomly drop words from the input
            new_s = [w for j, w in enumerate(words) if keep[j, i]]
            # we need to have at least one word in the sentence (more than the start / end sentence symbols)
            if len(new_s) == 1:
                new_s.append(words[np.random.randint(1, len(words))])
            assert len(new_s) >= 2
            sentences.append(new_s)
            lengths.append(len(new_s))
        # re-construct input
        l2 = torch.LongTensor(lengths).to(x.device)
        x2 = torch.LongTensor(l2.max(), l2.size(0)).to(x.device).fill_(self.train_loss.padding_idx)
        for i in range(l2.size(0)):
            x2[:l2[i], i].copy_(torch.LongTensor(sentences[i]).to(x.device))
        return x2.unsqueeze(2), l2

    def word_blank(self, x, l):
        """
        Randomly blank input words.
        """
        if self.params.word_blank == 0:
            return x, l
        assert 0 < self.params.word_blank < 1

        # define words to blank
        eos = self.params.eos_index
        assert (x[0] == eos).sum() == l.size(0)
        keep = np.random.rand(x.size(0) - 1, x.size(1)) >= self.params.word_blank
        keep[0] = 1  # do not blank the start sentence symbol

        sentences = []
        for i in range(l.size(0)):
            assert x[l[i] - 1, i] == eos
            words = x[:l[i] - 1, i].tolist()
            # randomly blank words from the input
            new_s = [w if keep[j, i] else self.params.mask_index for j, w in enumerate(words)]
            new_s.append(eos)
            assert len(new_s) == l[i] and new_s[0] == eos and new_s[-1] == eos
            sentences.append(new_s)
        # re-construct input
        x2 = torch.LongTensor(l.max(), l.size(0)).fill_(self.params.pad_index)
        for i in range(l.size(0)):
            x2[:l[i], i].copy_(torch.LongTensor(sentences[i]))
        return x2, l

    def add_noise(self, words, lengths):
        """
        Add noise to the encoder input.
        """
        words, lengths = self.word_shuffle(words, lengths)
        words, lengths = self.word_dropout(words, lengths)
        #words, lengths = self.word_blank(words, lengths)
        return words, lengths

    def validate(self, valid_iter, moving_average=None):
        """ Validate model.
            valid_iter: validate data iterator
        Returns:
            :obj:`nmt.Statistics`: validation loss statistics
        """
        valid_model = self.model
        if moving_average:
            # swap model params w/ moving average
            # (and keep the original parameters)
            model_params_data = []
            for avg, param in zip(self.moving_average,
                                  valid_model.parameters()):
                model_params_data.append(param.data)
                param.data = avg.data.half() if self.optim._fp16 == "legacy" \
                    else avg.data

        # Set model in validating mode.
        valid_model.eval()

        with torch.no_grad():
            stats = onmt.utils.Statistics()

            for batch in valid_iter:
                src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                                   else (batch.src, None)
                if self.denoise:
                    src, src_lengths = self.add_noise(src, src_lengths)

                tgt = batch.tgt
                if not self.use_feat_emb:
                    tgt = tgt[:, :, :1]

                # F-prop through the model.
                outputs, attns = valid_model(src, tgt, src_lengths,
                                             with_align=self.with_align)

                # Compute loss.
                _, batch_stats = self.valid_loss(batch, outputs, attns)

                # Update statistics.
                stats.update(batch_stats)
        if moving_average:
            for param_data, param in zip(model_params_data,
                                         self.model.parameters()):
                param.data = param_data

        # Set model back to training mode.
        valid_model.train()

        return stats

    def _gradient_accumulation(self, true_batches, normalization, total_stats,
                               report_stats):
        if self.accum_count > 1:
            self.optim.zero_grad()

        for k, batch in enumerate(true_batches):
            with torch.autocast('cuda') if self.model_dtype == 'fp16' else nullcontext():
                target_size = batch.tgt.size(0)
                # Truncated BPTT: reminder not compatible with accum > 1
                if self.trunc_size:
                    trunc_size = self.trunc_size
                else:
                    trunc_size = target_size

                src, src_lengths = batch.src if isinstance(batch.src, tuple) \
                    else (batch.src, None)

                if self.denoise:
                    src, src_lengths = self.add_noise(src, src_lengths)

                if src_lengths is not None:
                    report_stats.n_src_words += src_lengths.sum().item()

                tgt_outer = batch.tgt

                # overwrite the feature embeddings BOS token with the target
                # language token (at position 1) in case of multilingual training
                # with features
                if self.use_feat_emb:
                    seq_dim = 0 # if not batch_first else 1
                    assert(tgt_outer.size(seq_dim) > 1)
                    tgt_outer[0, :, 1] = tgt_outer[1, :, 1]

                bptt = False
                for j in range(0, target_size-1, trunc_size):
                    # 1. Create truncated target.
                    if self.use_feat_emb:
                        tgt = tgt_outer[j: j + trunc_size]
                    else:
                        tgt = tgt_outer[j: j + trunc_size, :, :1]
                    # 2. F-prop all but generator.
                    if self.accum_count == 1:
                        self.optim.zero_grad()

                    outputs, attns = self.model(src, tgt, src_lengths, bptt=bptt,
                                                with_align=self.with_align)
                    bptt = True
                    # 3. Compute loss.
                    try:
                        loss, batch_stats = self.train_loss(
                            batch,
                            outputs,
                            attns,
                            normalization=normalization,
                            shard_size=self.shard_size,
                            trunc_start=j,
                            trunc_size=trunc_size)

                        if loss is not None:
                            self.optim.backward(loss)

                        total_stats.update(batch_stats)
                        report_stats.update(batch_stats)

                    except Exception:
                        traceback.print_exc()
                        logger.info("At step %d, we removed a batch - accum %d",
                                    self.optim.training_step, k)

                # 4. Update the parameters and statistics.
                if self.accum_count == 1:
                    # Multi GPU gradient gather
                    if self.n_gpu > 1:
                        grads = [p.grad.data for p in self.model.parameters()
                                 if p.requires_grad
                                 and p.grad is not None]
                        onmt.utils.distributed.all_reduce_and_rescale_tensors(
                            grads, float(1))
                    self.optim.step()

                # If truncated, don't backprop fully.
                # TO CHECK
                # if dec_state is not None:
                #    dec_state.detach()
                if self.model.decoder.state is not None:
                    self.model.decoder.detach_state()

        # in case of multi step gradient accumulation,
        # update only after accum batches
        if self.accum_count > 1:
            if self.n_gpu > 1:
                grads = [p.grad.data for p in self.model.parameters()
                         if p.requires_grad
                         and p.grad is not None]
                onmt.utils.distributed.all_reduce_and_rescale_tensors(
                    grads, float(1))
            self.optim.step()

    def _start_report_manager(self, start_time=None):
        """
        Simple function to start report manager (if any)
        """
        if self.report_manager is not None:
            if start_time is None:
                self.report_manager.start()
            else:
                self.report_manager.start_time = start_time

    def _maybe_gather_stats(self, stat):
        """
        Gather statistics in multi-processes cases

        Args:
            stat(:obj:onmt.utils.Statistics): a Statistics object to gather
                or None (it returns None in this case)

        Returns:
            stat: the updated (or unchanged) stat object
        """
        if stat is not None and self.n_gpu > 1:
            return onmt.utils.Statistics.all_gather_stats(stat)
        return stat

    def _maybe_report_training(self, step, num_steps, learning_rate,
                               report_stats):
        """
        Simple function to report training stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_training` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_training(
                step, num_steps, learning_rate, report_stats,
                multigpu=self.n_gpu > 1)

    def _report_step(self, learning_rate, step, train_stats=None,
                     valid_stats=None):
        """
        Simple function to report stats (if report_manager is set)
        see `onmt.utils.ReportManagerBase.report_step` for doc
        """
        if self.report_manager is not None:
            return self.report_manager.report_step(
                learning_rate, step, train_stats=train_stats,
                valid_stats=valid_stats)
