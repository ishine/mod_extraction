import logging
import os
from collections import defaultdict
from typing import Any, Dict, Optional

import wandb
from matplotlib import pyplot as plt
from pytorch_lightning import Trainer, Callback, LightningModule
from pytorch_lightning.callbacks import LearningRateMonitor
from pytorch_lightning.loggers import WandbLogger
from torch import Tensor as T

from lfo_tcn.plotting import plot_spectrogram, plot_mod_sig, fig2img, plot_waveforms_stacked
from lfo_tcn.util import linear_interpolate_last_dim

logging.basicConfig()
log = logging.getLogger(__name__)
log.setLevel(level=os.environ.get("LOGLEVEL", "INFO"))


class ConsoleLRMonitor(LearningRateMonitor):
    # TODO(cm): enable every n steps
    def on_train_epoch_start(self,
                             trainer: Trainer,
                             *args: Any,
                             **kwargs: Any) -> None:
        super().on_train_epoch_start(trainer, *args, **kwargs)
        if self.logging_interval != "step":
            interval = "epoch" if self.logging_interval is None else "any"
            latest_stat = self._extract_stats(trainer, interval)
            latest_stat_str = {k: f"{v:.8f}" for k, v in latest_stat.items()}
            if latest_stat:
                log.info(f"Current LR: {latest_stat_str}")


class LogSpecAndModSigCallback(Callback):
    def __init__(self, n_examples: int = 5, log_wet_hat: bool = False) -> None:
        super().__init__()
        self.n_examples = n_examples
        self.log_wet_hat = log_wet_hat
        self.images = []

    def on_validation_batch_end(self,
                                trainer: Trainer,
                                pl_module: LightningModule,
                                outputs: (T, Dict[str, T], Optional[Dict[str, T]]),
                                batch: (T, T, T, Dict[str, T]),
                                batch_idx: int,
                                dataloader_idx: int) -> None:
        if outputs is None:
            return
        _, data_dict, fx_params = outputs
        wet = data_dict["wet"]
        wet_hat = data_dict.get("wet_hat", None)
        mod_sig_hat = data_dict["mod_sig_hat"]
        mod_sig = data_dict["mod_sig"]
        n_batches = mod_sig.size(0)
        if batch_idx == 0:
            self.images = []
            for idx in range(self.n_examples):
                if idx < n_batches:
                    if self.log_wet_hat and wet_hat is not  None:
                        fig, ax = plt.subplots(nrows=3, figsize=(6, 15), sharex="all", squeeze=True)
                        w_hat = wet_hat[idx]
                        plot_spectrogram(w_hat, ax[1], sr=pl_module.sr)
                    else:
                        fig, ax = plt.subplots(nrows=2, figsize=(6, 10), sharex="all", squeeze=True)
                    params = {k: v[idx] for k, v in fx_params.items()}
                    title = ", ".join([f"{k}: {v:.2f}" for k, v in params.items()
                                       if k not in {"mix", "rate_hz"}])
                    title = f"{idx}: {title}"
                    w = wet[idx]
                    spec = plot_spectrogram(w, ax[0], title, sr=pl_module.sr)
                    n_frames = spec.size(-1)
                    m_hat = mod_sig_hat[idx]
                    if m_hat.size(-1) != n_frames:
                        m_hat = linear_interpolate_last_dim(m_hat, n_frames)
                    m = mod_sig[idx]
                    if m.size(-1) != n_frames:
                        m = linear_interpolate_last_dim(m, n_frames)
                    plot_mod_sig(ax[-1], m_hat, m)
                    fig.tight_layout()
                    img = fig2img(fig)
                    self.images.append(img)

    def on_validation_epoch_end(self, trainer: Trainer, pl_module: LightningModule) -> None:
        if self.images:
            for logger in trainer.loggers:
                if isinstance(logger, WandbLogger):
                    logger.log_image(key="mod_sig_plots",
                                     images=self.images,
                                     step=trainer.global_step)


class LogAudioCallback(Callback):
    def __init__(self, n_examples: int = 5, log_dry_audio: bool = False) -> None:
        super().__init__()
        self.n_examples = n_examples
        self.log_dry_audio = log_dry_audio
        self.dry_audio = []
        self.wet_audio = []
        self.wet_hat_audio = []
        self.images = []

    def on_validation_batch_end(self,
                                trainer: Trainer,
                                pl_module: LightningModule,
                                outputs: (T, Dict[str, T], Dict[str, T]),
                                batch: (T, T, T, Dict[str, T]),
                                batch_idx: int,
                                dataloader_idx: int) -> None:
        if outputs is None:
            return
        _, data_dict, fx_params = outputs
        dry = data_dict["dry"]
        wet = data_dict["wet"]
        wet_hat = data_dict.get("wet_hat")
        n_batches = dry.size(0)
        if batch_idx == 0:
            self.images = []
            self.dry_audio = []
            self.wet_audio = []
            self.wet_hat_audio = []
            for idx in range(self.n_examples):
                if idx < n_batches:
                    d = dry[idx]
                    w = wet[idx]
                    w_hat = wet_hat[idx]
                    params = {k: v[idx] for k, v in fx_params.items()}
                    title = ", ".join([f"{k}: {v:.2f}" for k, v in params.items()
                                       if k not in {"mix", "rate_hz"}])
                    title = f"{idx}: {title}"
                    if self.log_dry_audio:
                        waveforms = [d, w, w_hat]
                        labels = ["dry", "wet", "wet_hat"]
                    else:
                        waveforms = [w, w_hat]
                        labels = ["wet", "wet_hat"]

                    fig = plot_waveforms_stacked(waveforms, pl_module.sr, title, labels)
                    img = fig2img(fig)
                    self.images.append(img)
                    self.dry_audio.append(d.swapaxes(0, 1).numpy())
                    self.wet_audio.append(w.swapaxes(0, 1).numpy())
                    self.wet_hat_audio.append(w_hat.swapaxes(0, 1).numpy())

    def on_validation_epoch_end(self, trainer: Trainer, pl_module: LightningModule) -> None:
        for logger in trainer.loggers:
            if isinstance(logger, WandbLogger):
                if isinstance(logger, WandbLogger):
                    # TODO(cm): combine into one table
                    logger.log_image(key="audio_plots",
                                     images=self.images,
                                     step=trainer.global_step)
                    data = defaultdict(list)
                    columns = []
                    for idx, (d, w, w_hat) in enumerate(zip(self.dry_audio, self.wet_audio, self.wet_hat_audio)):
                        columns.append(f"idx_{idx}")
                        if self.log_dry_audio:
                            data["dry"].append(wandb.Audio(d, caption=f"dry_{idx}", sample_rate=int(pl_module.sr)))
                        data["wet"].append(wandb.Audio(w, caption=f"wet_{idx}", sample_rate=int(pl_module.sr)))
                        data["wet_hat"].append(wandb.Audio(w_hat, caption=f"wet_hat_{idx}", sample_rate=int(pl_module.sr)))

                    data = list(data.values())
                    logger.log_table(key="audio", columns=columns, data=data, step=trainer.global_step)
