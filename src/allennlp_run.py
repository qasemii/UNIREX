import os, shutil
from typing import Tuple, Optional

import torch
import pytorch_lightning as pl
from hydra.utils import instantiate
from omegaconf import open_dict, DictConfig
from pytorch_lightning.callbacks import (
    ModelCheckpoint, EarlyStopping
)
from transformers import AutoTokenizer

from src.utils.data import dataset_info, monitor_dict
from src.utils.logging import get_logger
from src.utils.callbacks import BestPerformance
from src.utils.expl import attr_algos, baseline_required

from allennlp.confidence_checks.task_checklists import TextualEntailmentSuite


def get_callbacks(cfg: DictConfig):

    monitor = monitor_dict[cfg.data.dataset]
    mode = cfg.data.mode
    callbacks = [
        BestPerformance(monitor=monitor, mode=mode)
    ]

    if cfg.save_checkpoint:
        callbacks.append(
            ModelCheckpoint(
                monitor=monitor,
                dirpath=os.path.join(cfg.save_dir, 'checkpoints'),
                save_top_k=1,
                mode=mode,
                verbose=True,
                save_last=False,
                save_weights_only=True,
            )
        )

    if cfg.early_stopping:
        callbacks.append(
            EarlyStopping(
                monitor=monitor,
                min_delta=0.00,
                patience=cfg.training.patience,
                verbose=False,
                mode=mode
            )
        )

    return callbacks


logger = get_logger(__name__)


def build(cfg) -> Tuple[pl.LightningDataModule, pl.LightningModule, pl.Trainer]:
    model = instantiate(
        cfg.model, num_classes=dataset_info['esnli']['num_classes'],
        neg_weight=cfg.data.neg_weight,
        _recursive_=False
    )
    logger.info(f'load {cfg.model.arch} <{cfg.model._target_}>')

    run_logger = instantiate(cfg.logger, cfg=cfg, _recursive_=False)

    with open_dict(cfg):
        if cfg.debug or cfg.logger.offline:
            exp_dir = cfg.logger.name
            cfg.logger.neptune_exp_id = cfg.logger.name
        else:
            if cfg.logger.logger == "neptune":
                exp_dir = run_logger.experiment_id
                cfg.logger.neptune_exp_id = run_logger.experiment_id
            else:
                raise NotImplementedError
        cfg.save_dir = os.path.join(cfg.save_dir, exp_dir)
        os.makedirs(cfg.save_dir, exist_ok=True)

        # copy hydra configs
        shutil.copytree(
            os.path.join(os.getcwd(), ".hydra"),
            os.path.join(cfg.save_dir, "hydra")
        )

    logger.info(f"saving to {cfg.save_dir}")

    trainer = instantiate(
        cfg.trainer,
        callbacks=get_callbacks(cfg),
        checkpoint_callback=cfg.save_checkpoint,
        logger=run_logger,
        _convert_="all",
    )

    return model, trainer


def restore_config_params(model, cfg: DictConfig):
    for key, val in cfg.model.items():
        setattr(model, key, val)

    if cfg.model.save_outputs:
        assert cfg.model.exp_id in cfg.training.ckpt_path

    if cfg.model.explainer_type == 'attr_algo' and model.attr_algo in attr_algos.keys():
        model.attr_func = attr_algos[model.attr_algo](model)
        model.tokenizer = AutoTokenizer.from_pretrained(cfg.model.arch)
        model.baseline_required = baseline_required[model.attr_algo]
        model.word_emb_layer = model.task_encoder.embeddings.word_embeddings
        model.attr_dict['baseline_required'] = model.baseline_required
        if model.attr_algo == 'integrated-gradients':
            model.attr_dict['ig_steps'] = getattr(model, 'ig_steps')
            model.attr_dict['internal_batch_size'] = getattr(model, 'internal_batch_size')
            model.attr_dict['return_convergence_delta'] = getattr(model, 'return_convergence_delta')
        elif model.attr_algo == 'gradient-shap':
            model.attr_dict['gradshap_n_samples'] = getattr(model, 'gradshap_n_samples')
            model.attr_dict['gradshap_stdevs'] = getattr(model, 'gradshap_stdevs')
        model.attr_dict['attr_func'] = model.attr_func
        model.attr_dict['tokenizer'] = model.tokenizer

    logger.info('Restored params from model config.')

    return model


def run(cfg: DictConfig) -> Optional[float]:
    pl.seed_everything(cfg.seed)
    model, trainer = build(cfg)
    pl.seed_everything(cfg.seed)

    # evaluate the pretrained model on the provided splits
    assert cfg.training.ckpt_path
    save_dir = '/'.join(cfg.save_dir.split('/')[:-2])
    ckpt_path = os.path.join(save_dir, cfg.training.ckpt_path)
    model = model.load_from_checkpoint(ckpt_path, strict=False)
    logger.info(f"Loaded checkpoint for evaluation from {cfg.training.ckpt_path}")
    model = restore_config_params(model, cfg)
    print('Evaluating loaded model checkpoint...')

    suite = TextualEntailmentSuite()
    suite.run(model, max_examples=15)




