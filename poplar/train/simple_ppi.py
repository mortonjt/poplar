import os
import time
import datetime
import numpy as np
from tqdm import tqdm
import torch
import torch.optim as optim
from fairseq.models.roberta import RobertaModel
from poplar.model.transformer import RobertaConstrastiveHead
from poplar.dataset.interactions import InteractionDataDirectory
from poplar.util import encode, tokenize
from torch.utils.tensorboard import SummaryWriter
from torch.nn.utils import clip_grad_norm_
from transformers import AdamW, WarmupLinearSchedule


def simple_ppitrain(
        pretrained_model, directory_dataloader,
        logging_path=None,
        emb_dimension=100, max_steps=0,
        learning_rate=5e-5, warmup_steps=1000,
        gradient_accumulation_steps=1,
        clip_norm=10., summary_interval=100, checkpoint_interval=100,
        model_path='model', device=None):
    """ Train the roberta model

    Parameters
    ----------
    pretrained_model : fairseq.models.roberta.RobertaModel
        Pretrained Roberta model.
    directory_dataloader : InteractionDataDirectory
        Creates dataloaders
    emb_dimension : int
        Number of dimensions to train the model.
    logging_path : path
        Path of logging file.
    max_steps : int
        Maximum number of steps to run for. Each step corresponds to
        the evaluation of a protein pair. If this is zero, then it'll
        default to one epochs worth of protein pairs (ie one pass through
        all of the protein pairs in the training dataset).
    learning_rate : float
        Learning rate of ADAM
    warmup_steps : int
        Number of warmup steps for scheduler
    clip_norm : float
        Clipping norm of gradients
    summary_interval : int
        Number of steps before saving summary.
    checkpoint_interval : int
        Number of steps before saving checkpoint.
    device : str
        Name of device to run (specifies gpu or not)

    Returns
    -------
    finetuned_model : poplar.transformer.RobertaConstrastiveHead
    """
    last_summary_time = time.time()
    last_checkpoint_time = time.time()
    # the dimensionality of the roberta model
    roberta_dim = int(list(list(pretrained_model.parameters())[-1].shape)[0])
    finetuned_model = RobertaConstrastiveHead(roberta_dim, emb_dimension)

    # optimizer = optim.Adamax(finetuned_model.parameters(), betas=betas)
    optimizer = AdamW(finetuned_model.parameters(), lr=learning_rate)
    # uncomment for production ready code
    if max_steps > 0:
        num_data = directory_dataloader.total()
        t_total = (max_steps // gradient_accumulation_steps) + 1
        epochs = t_total // num_data
    else:
        num_data = directory_dataloader.total()
        t_total = num_data // gradient_accumulation_steps
        epochs = 1

    # test run
    # num_data = 3e6
    # t_total = (max_steps // gradient_accumulation_steps) + 1
    # epochs = int(t_total // num_data)

    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=warmup_steps, t_total=t_total)
    # quick and dirty scheduler
    #scheduler = WarmupLinearSchedule(optimizer, warmup_steps=warmup_steps, t_total=300000 * 31)
    finetuned_model.to(device)
    n_gpu = torch.cuda.device_count()
    print(os.environ["CUDA_VISIBLE_DEVICES"], 'devices available')
    print("Utilizing ", torch.cuda.device_count(), device)
    if n_gpu > 1:
        finetuned_model = torch.nn.DataParallel(finetuned_model)

    # Initialize logging path
    if logging_path is None:
        basename = "logdir"
        suffix = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
        logging_path = "_".join([basename, suffix])
    it = 0
    writer = SummaryWriter(logging_path)

    # metrics to report
    err, cv_err, tpr, pos_score, batch_size = 0, 0, 0, 0, 0

    now = time.time()
    last_now = time.time()
    print('Number of pairs', num_data)
    print('Number datasets', len(directory_dataloader))
    print('Number of epochs', epochs)
    for e in range(epochs):
        for k, dataloader in enumerate(directory_dataloader):
            finetuned_model.train()
            train_dataloader, test_dataloader = dataloader
            num_batches = len(train_dataloader)
            num_cv_batches = len(test_dataloader)
            batch_size = train_dataloader.batch_size

            print(f'dataset {k}, num_batches {num_batches}, num_cvs {num_cv_batches}, '
                  f'seconds / batch {now - last_now}')
            for j, (gene, pos, neg) in enumerate(train_dataloader):
                last_now = now
                now = time.time()

                g, p, n = tokenize(gene, pos, neg, pretrained_model, device)
                loss = finetuned_model.forward(g, p, n)

                if n_gpu > 1:
                    loss = loss.mean()
                if gradient_accumulation_steps > 1:
                    loss = loss / gradient_accumulation_steps
                loss.backward()
                clip_grad_norm_(finetuned_model.parameters(), clip_norm)

                it += len(gene)
                err = loss.item()
                print(f'epoch {e}, dataset {k}, batch {j}, err {err}, total batches {num_batches}, '
                      f'seconds / batch {now - last_now}')

                # write down summary stats
                if (now - last_summary_time) > summary_interval:
                    # add gradients to histogram
                    writer.add_scalar('train_error', err, it)
                    for name, param in finetuned_model.named_parameters():
                        writer.add_histogram('grad/%s' %  name, param.grad, it)
                    last_summary_time = now

                # clean up
                del loss, g, p, n
                if 'cuda' in device:
                    torch.cuda.empty_cache()

                if (now - last_checkpoint_time) > checkpoint_interval:
                    suffix = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
                    model_path_ = model_path + suffix
                    # for parallel training
                    try:
                        state_dict = finetuned_model.module.state_dict()
                    except AttributeError:
                        state_dict = finetuned_model.state_dict()
                    torch.save(state_dict, model_path_)
                    last_checkpoint_time = now

                # accumulate gradients - so that we do backprop after loss
                # has been calculated on entire batch
                if j % gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()
                    finetuned_model.zero_grad()

            # cross validation after each dataset is processed
            with torch.no_grad():
                cv_err, pos_score, rank_counts = 0, 0, 0
                batch_size = test_dataloader.batch_size
                for j, (cv_gene, cv_pos, cv_neg) in enumerate(test_dataloader):
                    gv, pv, nv = tokenize(cv_gene, cv_pos, cv_neg,
                                          pretrained_model, device)
                    cv_score = finetuned_model.forward(gv, pv, nv)
                    pred_pos = finetuned_model.predict(gv, pv)
                    pred_neg = finetuned_model.predict(gv, nv)
                    cv_err += cv_score.item()
                    pos_score += torch.sum(pred_pos).item()
                    rank_counts += torch.sum(pred_pos > pred_neg).item()
                    print(f'epoch {e}, dataset {k}, batch {j}, cv_err {cv_err}, '
                          f'rank_counts {rank_counts}, '
                          f'total batches {num_cv_batches}, '
                          f'seconds / batch {now - last_now}')

                    #clean up
                    del pred_pos, pred_neg, cv_score, gv, pv, nv
                    if 'cuda' in device:
                        torch.cuda.empty_cache()

                if len(test_dataloader) > 0:
                    cv_err = cv_err / len(test_dataloader)
                    avg_rank = rank_counts / len(test_dataloader)
                    tpr = avg_rank / batch_size
                    pos_score = pos_score / len(test_dataloader)
                    print(f'epoch {e}, dataset {k}, batch {j}, cv_err {cv_err}, '
                          f'rank_counts {avg_rank}, tpr {tpr}, '
                          f'pos_score {pos_score}, '
                          f'total batches {num_cv_batches}, '
                          f'seconds / batch {now - last_now}')
                    writer.add_scalar('test_error', cv_err, it)
                    writer.add_scalar('TPR', tpr, it)
                    writer.add_scalar('pos_score', pos_score, it)

    # save hparams
    writer.add_hparams(
        hparam_dict={
            'emb_dimension': emb_dimension,
            'learning_rate': learning_rate,
            'warmup_steps': warmup_steps,
            'gradient_accumulation_steps': gradient_accumulation_steps,
            'batch_size': batch_size
            },
        metric_dict={
            'train_error': err,
            'test_error': cv_err,
            'TPR': tpr,
            'pos_score': pos_score
        }
    )

    writer.close()
    return finetuned_model


def simple_ppirun(
        fasta_file, links_directory,
        checkpoint_path, data_dir, model_path, logging_path,
        training_column='Training',
        emb_dimension=100, num_neg=10,
        max_steps=10, learning_rate=5e-5,
        warmup_steps=1000, gradient_accumulation_steps=1,
        clip_norm=10, batch_size=10, num_workers=10,
        summary_interval=1, checkpoint_interval=1000,
        device='cpu'):
    """ Train interaction model

    Parameters
    ----------
    fasta_file : filepath
        Fasta file of sequences of interest.
    links_directory : filepath
        Directory of links files. Each link file contains
        a table of tab delimited interactions
    emb_dimensions : int
        Number of embedding dimensions.
    num_neg : int
        Number of negative samples.
    max_steps : int
        Maximum number of steps to run for. Each step corresponds to
        the evaluation of a protein pair.
    learning_rate : float
        Learning rate of ADAM
    warmup_steps : int
        Number of warmup steps for scheduler
    checkpoint_path : path
        Path for roberta model.
    data_dir : path
        Path to data used for pretraining.
    model_path : path
        Path for finetuned model.
    logging_path : path
        Path for logging information.
    clip_norm : float
        Clipping norm of gradients
    batch_size : int
        Number of protein triples to analyze in a given batch.
    summary_interval : int
        Number of seconds for a summary update.
    device : str
        Name of device to run on.
    """
    # An example of how to load your own roberta model
    # roberta_checkpoint_path = 'checkpoints/uniref50'
    # data_dir = 'data/uniref50'
    # pytorch_dump_folder_path = 'checkpoints/roberta_TF_dump'
    # classification_head = False
    # roberta = FairseqRobertaModel.from_pretrained(
    #     roberta_checkpoint_path, 'checkpoint_best.pt', data_dir)

    pretrained_model = RobertaModel.from_pretrained(
        checkpoint_path, 'checkpoint_best.pt', data_dir)
    pretrained_model.to(device)

    # freeze the weights of the pre-trained model
    for param in pretrained_model.parameters():
        param.requires_grad = False

    n_gpu = torch.cuda.device_count()
    batch_size = max(batch_size, batch_size * n_gpu)
    print('batch_size', batch_size)
    interaction_directory = InteractionDataDirectory(
        fasta_file, links_directory, training_column,
        batch_size, num_workers, device
    )

    # train the fine_tuned model parameters
    finetuned_model = train(
        pretrained_model, interaction_directory,
        logging_path,
        emb_dimension, max_steps,
        learning_rate, warmup_steps, gradient_accumulation_steps,
        clip_norm, summary_interval, checkpoint_interval,
        model_path, device)

    # save the last model checkpoint
    suffix = 'last'
    model_path_ = model_path + suffix
    torch.save(finetuned_model.state_dict(), model_path_)