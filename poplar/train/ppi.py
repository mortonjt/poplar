import os
import time
import datetime
import numpy as np
from tqdm import tqdm
import torch
import torch.optim as optim
from fairseq.models.roberta import RobertaModel
from poplar.model.ppibinder import PPIBinder
from poplar.dataset.interactions import InteractionDataDirectory
from poplar.dataset.interactions import ValidationDataset
from poplar.dataset.interactions import NegativeSampler
from poplar.util import encode, tokenize
from poplar.evaluate import pairwise_auc
from poplar.summary import (
    summarize_gradients, checkpoint, initialize_logging)
from torch.utils.tensorboard import SummaryWriter
from torch.nn.utils import clip_grad_norm_
from transformers import AdamW, WarmupLinearSchedule


def train(ppi_model, directory_dataloader,
          logging_path=None, emb_dimension=100, max_steps=0,
          learning_rate=5e-5, warmup_steps=1000,
          gradient_accumulation_steps=1,
          clip_norm=10., summary_interval=100, checkpoint_interval=100,
          model_path='model', device='cpu'):
    """ Train the protein-protein interaction model.

    Parameters
    ----------
    ppi_model : fairseq.models.roberta.RobertaModel
        Protein interaction prediction model
    directory_dataloader : InteractionDataDirectory
        Creates dataloaders.
    *positive_dataloaders : list of dataloaders
        List of torch dataloaders for interactions. TODO!
    *negative_dataloaders : list of dataloaders
        List of torch dataloaders for interactions. TODO!
    logging_path : path
        Path of logging file.
    emb_dimension : int
        Number of dimensions to train the model.
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
    finetuned_model : poplar.ppbinder.PPBinder

    TODO
    ----
    1. Enable positive dataloaders. (done)
    2. Enable negative dataloaders. (done)
    3. Update the run scripts.
    4. Preferably come up with a better name rather than simple_ppi
       (i.e. binary contact, binary-binding) (done)
    5. Include language model inside the prediction model?
       May make it easier to modularize. (done)
    6. Add tests for remaining evaluation functions.
    7. Maybe within the model fold create subfolders
       `peptide`, `fold` and `bind`, with abstract classes to
       allow for plug and play architectures.
    """
    last_summary_time, last_checkpoint_time = time.time(), time.time()

    # Estimate running time
    num_data = directory_dataloader.total()
    t_total = num_data // gradient_accumulation_steps
    max_steps = max(1, max_steps)
    epochs = max_steps // num_data

    optimizer = AdamW(ppi_model.parameters(), lr=learning_rate)
    scheduler = WarmupLinearSchedule(
        optimizer, warmup_steps=warmup_steps, t_total=t_total)

    # Initialize logging path
    writer = initialize_logging(logging_path=None)
    it = 0  # number of steps (iterations)
    print('Number of pairs', num_data)
    print('Number datasets', len(directory_dataloader))
    print('Number of epochs', epochs)
    # converts sequences to peptide encodings
    encode_f = lambda x: torch.stack(list(map(encode, x)))
    for e in range(epochs):
        for k, dataloader in enumerate(directory_dataloader):
            ppi_model.train()
            train_dataloader, test_dataloader, valid_dataloader = dataloader
            num_batches = len(train_dataloader)
            batch_size = train_dataloader.batch_size

            print(f'dataset {k}, num_batches {num_batches}')
            for j, (gene, pos, neg) in enumerate(train_dataloader):
                # TODO: Need to work on encoding gene, pos and neg
                g = ppi_model.encode(gene)
                p = ppi_model.encode(pos)
                n = ppi_model.encode(neg)
                loss = ppi_model.forward(g, p, n)

                if torch.cuda.device_count() > 1:
                    loss = loss.mean()
                if gradient_accumulation_steps > 1:
                    loss = loss / gradient_accumulation_steps
                loss.backward()
                clip_grad_norm_(ppi_model.parameters(), clip_norm)

                it += len(gene)
                err = loss.item()

                # write down summary stats
                last_summary_time = summarize_gradients(
                    ppi_model, summary_interval,
                    last_summary_time, it, writer)

                # clean up
                del loss, g, p, n
                if 'cuda' in device:
                    torch.cuda.empty_cache()

                # checkpoint
                last_checkpoint_time = checkpoint(
                    ppi_model, logging_path, checkpoint_interval,
                    last_checkpoint_time, writer)

                # accumulate gradients - so that we do backprop after loss
                # has been calculated on entire batch
                if j % gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()
                    ppi_model.zero_grad()

            # cross validation after each dataset is processed
            tpr = pairwise_auc(ppi_model, test_dataloader,
                               'Main/test', it, writer, device)


    # save hparams (TODO: hparams isn't importing correctly)
    # writer.add_hparams(
    #     hparam_dict={
    #         'emb_dimension': emb_dimension,
    #         'learning_rate': learning_rate,
    #         'warmup_steps': warmup_steps,
    #         'gradient_accumulation_steps': gradient_accumulation_steps,
    #         'batch_size': batch_size
    #         },
    #     metric_dict={
    #         'train_error': err,
    #         'test_error': cv_err,
    #         'TPR': tpr,
    #         'pos_score': pos_score
    #     }
    # )

    writer.close()
    return ppi_model



def ppi(fasta_file, training_directory,
        checkpoint_path, data_dir, model_path, logging_path,
        training_column=4,
        emb_dimension=100, num_neg=10,
        max_steps=10, learning_rate=5e-5,
        warmup_steps=1000, gradient_accumulation_steps=1,
        clip_norm=10, batch_size=10, num_workers=10,
        summary_interval=1, checkpoint_interval=1000,
        device='cpu'):
    """ Train protein-protein interaction model

    Parameters
    ----------
    fasta_file : filepath
        Fasta file of sequences of interest.
    training_directory : filepath
        Directory of links files. Each link file contains
        a table of tab delimited interactions
    positive_test_datasets : list of filepaths
        List of datasets to use for testing positive interactions.
    negative_test_datasets : list of filepaths
        List of datasets to use for testing negative interactions.
    emb_dimensions : int
        Number of embedding dimensions.
    num_neg : int
        Number of negative samples.
    max_steps : int
        Maximum number of steps to run for. Each step corresponds to
z        the evaluation of a protein pair.
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

    TODO
    ----
    Enable per test dataset dataloaders
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
    # the dimensionality of the roberta model
    roberta_dim = int(list(list(pretrained_model.parameters())[-1].shape)[0])
    # freeze the weights of the pre-trained model
    for param in pretrained_model.parameters():
        param.requires_grad = False

    ppi_model = PPIBinder(roberta_dim, emb_dimension, pretrained_model)
    ppi_model.to(device)

    n_gpu = torch.cuda.device_count()
    if "CUDA_VISIBLE_DEVICES" in os.environ:
        print(os.environ["CUDA_VISIBLE_DEVICES"], 'devices available')
        print("Utilizing ", torch.cuda.device_count(), device)
        if n_gpu > 1:
            ppi_model = torch.nn.DataParallel(ppi_model)

    batch_size = max(batch_size, batch_size * n_gpu)
    interaction_directory = InteractionDataDirectory(
        fasta_file, training_directory, training_column,
        batch_size, num_workers, 'cuda' in device
    )
    sampler = NegativeSampler(fasta_file)

    # TODO: add in positive and negative dataloaders

    # train the fine_tuned model parameters
    finetuned_model = train(
        ppi_model=ppi_model, directory_dataloader=interaction_directory,
        logging_path=logging_path, emb_dimension=emb_dimension,
        max_steps=max_steps, learning_rate=learning_rate,
        warmup_steps=warmup_steps,
        gradient_accumulation_steps=gradient_accumulation_steps,
        clip_norm=clip_norm, summary_interval=summary_interval,
        checkpoint_interval=checkpoint_interval,
        model_path=model_path, device=device)

    # save the last model checkpoint
    suffix = 'last'
    model_path_ = model_path + suffix
    torch.save(finetuned_model.state_dict(), model_path_)
