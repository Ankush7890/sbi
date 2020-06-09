from __future__ import annotations

from abc import ABC, abstractmethod

from copy import deepcopy
from sbi.utils.sbiutils import warn_on_invalid_x
from sbi.utils import clamp_and_warn
from typing import Callable, Dict, Optional, Union

import numpy as np
import torch
from torch import Tensor, nn, optim, ones, eye
from torch.nn.utils import clip_grad_norm_

from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.tensorboard import SummaryWriter

import sbi.utils as utils
from sbi.inference.base import NeuralInference
from sbi.inference.posterior import NeuralPosterior
from sbi.utils.torchutils import ensure_x_batched, ensure_theta_batched
from sbi.types import ScalarFloat, OneOrMore
from sbi.utils import handle_invalid_x, warn_on_invalid_x


class RatioEstimator(NeuralInference, ABC):
    def __init__(
        self,
        simulator: Callable,
        prior,
        x_shape: Optional[torch.Size] = None,
        classifier: Optional[nn.Module] = None,
        simulation_batch_size: int = 1,
        mcmc_method: str = "slice_np",
        summary_net: Optional[nn.Module] = None,
        retrain_from_scratch_each_round: bool = False,
        num_workers: int = 1,
        summary_writer: Optional[SummaryWriter] = None,
        device: Optional[torch.device] = None,
        skip_input_checks: bool = False,
        show_progressbar: bool = True,
        show_round_summary: bool = False,
        logging_level: Union[int, str] = "warning",
        exclude_invalid_x: bool = False,
    ):
        r"""Sequential Ratio Estimation [1]

        Args:
            classifier: Binary classifier.
            retrain_from_scratch_each_round: Whether to retrain the conditional
                density estimator for the posterior from scratch each round.
            summary_net: Optional network which may be used to produce feature
                vectors f(x) for high-dimensional simulation outputs $x$.
            classifier_loss: `sre` implements the algorithm suggested in Durkan et al.
                2020, whereas `aalr` implements the algorithm suggested in
                Hermans et al. 2020. `sre` can use more than two atoms, potentially
                boosting performance, but does not allow for exact posterior density
                evaluation (only up to a normalizing constant), even when training
                only one round. `aalr` is limited to `num_atoms=2`, but allows for
                density evaluation when training for one round.

        See docstring of `NeuralInference` class for all other arguments.
        """

        super().__init__(
            simulator=simulator,
            prior=prior,
            x_shape=x_shape,
            simulation_batch_size=simulation_batch_size,
            device=device,
            summary_writer=summary_writer,
            num_workers=num_workers,
            skip_input_checks=skip_input_checks,
            show_progressbar=show_progressbar,
            show_round_summary=show_round_summary,
            logging_level=logging_level,
            exclude_invalid_x=exclude_invalid_x,
        )

        if classifier is None:
            classifier = utils.classifier_nn(
                model="resnet",
                theta_shape=self._prior.sample().shape,
                x_o_shape=self._x_shape,
            )

        # Create posterior object which can sample().
        algorithm_family = self.__class__.__name__.lower()

        self._posterior = NeuralPosterior(
            algorithm_family=algorithm_family,
            neural_net=classifier,
            prior=self._prior,
            mcmc_method=mcmc_method,
            get_potential_function=PotentialFunctionProvider(),
            x_shape=self._x_shape,
        )

        self._posterior.net.train(True)

        # We may want to summarize high-dimensional x.
        # This may be either a fixed or learned transformation.
        self._summary_net = nn.Identity() if summary_net is None else summary_net

        # If we're retraining from scratch each round,
        # keep a copy of the original untrained model for reinitialization.
        self._retrain_from_scratch_each_round = retrain_from_scratch_each_round
        if self._retrain_from_scratch_each_round:
            self._untrained_classifier = deepcopy(classifier)
        else:
            self._untrained_classifier = None

        # Ratio-based-specific summary_writer fields.
        self._summary.update({"mcmc_times": []})  # type: ignore

    def __call__(
        self,
        num_rounds: int,
        num_simulations_per_round: OneOrMore[int],
        x_o: Optional[Tensor] = None,
        num_atoms: int = 10,
        batch_size: int = 100,
        learning_rate: float = 5e-4,
        validation_fraction: float = 0.1,
        stop_after_epochs: int = 20,
        max_num_epochs: Optional[int] = None,
        clip_max_norm: Optional[float] = 5.0,
    ) -> NeuralPosterior:
        """Run SRE

        This runs SRE for num_rounds rounds, using num_simulations_per_round calls to
        the simulator

        Args:
            num_rounds: Number of rounds to run
            num_simulations_per_round: Number of simulator calls per round
            x_o: When SRE is used in a multi-round setting, training samples for every
                round after the first round are drawn from the most recent posterior
                estimate given $x_o$, i.e. $p(\theta|x_o)$.
            num_atoms: Number of atoms to use for classification.
            batch_size: Size of batch to use for training.
            learning_rate: Learning rate for Adam optimizer.
            validation_fraction: The fraction of data to use for validation.
            stop_after_epochs: The number of epochs to wait for improvement on the
                validation set before terminating training.
            max_num_epochs: Maximum number of epochs to run. If max_num_epochs
                is reached, we stop training even if the validation loss is still
                decreasing.
            clip_max_norm: Value at which to clip the total gradient norm in order to
                prevent exploding gradients. Use None for no clipping.

        Returns:
            Posterior that can be sampled and evaluated.
        """

        self._handle_x_o_wrt_amortization(x_o, num_rounds)

        max_num_epochs = 2 ** 31 - 1 if max_num_epochs is None else max_num_epochs

        num_sims_per_round = self._ensure_list(num_simulations_per_round, num_rounds)

        for round_, num_sims in enumerate(num_sims_per_round):

            # Generate theta from prior in first round, and from most recent posterior
            # estimate in subsequent rounds.
            if round_ == 0:
                theta = self._prior.sample((num_sims,))
            else:
                theta = self._posterior.sample(
                    num_sims, show_progressbar=self._show_progressbar
                )

            # Why do we return theta just below? When using multiprocessing, the thetas
            # are not handled sequentially anymore. Hence, the x that are returned do
            # not necessarily have the same order as the theta we define above. We
            # therefore return a theta vector with the same ordering as x.
            theta, x = self._batched_simulator(theta)

            # Check for NaNs in simulations.
            is_valid_x, num_nans, num_infs = handle_invalid_x(x, self.exclude_invalid_x)
            warn_on_invalid_x(num_nans, num_infs, self.exclude_invalid_x)

            # Store (theta, x) pairs.
            self._theta_bank.append(theta[is_valid_x])
            self._x_bank.append(x[is_valid_x])

            # Fit posterior using newly aggregated data set.
            self._train(
                num_atoms=num_atoms,
                batch_size=batch_size,
                learning_rate=learning_rate,
                validation_fraction=validation_fraction,
                stop_after_epochs=stop_after_epochs,
                max_num_epochs=max_num_epochs,
                clip_max_norm=clip_max_norm,
            )

            # Update description for progress bar.
            if self._show_round_summary:
                print(self._describe_round(round_, self._summary))

            # Update tensorboard and summary dict.
            self._summarize(
                round_=round_,
                x_o=self._posterior.x_o,
                theta_bank=self._theta_bank,
                x_bank=self._x_bank,
            )

        self._posterior._num_trained_rounds = num_rounds
        return self._posterior

    def _train(
        self,
        num_atoms: int,
        batch_size: int,
        learning_rate: float,
        validation_fraction: float,
        stop_after_epochs: int,
        max_num_epochs: int,
        clip_max_norm: Optional[float],
    ) -> None:
        r"""
        Trains the neural classifier.

        Update the classifier weights by maximizing a Bernoulli likelihood which
        distinguishes between jointly distributed $(\theta, x)$ pairs and randomly
        chosen $(\theta, x)$ pairs.

        Uses performance on a held-out validation set as a terminating condition (early
        stopping).
        """

        # Get total number of training examples.
        num_examples = sum(len(theta) for theta in self._theta_bank)

        # Select random train and validation splits from (theta, x) pairs.
        permuted_indices = torch.randperm(num_examples)
        num_training_examples = int((1 - validation_fraction) * num_examples)
        num_validation_examples = num_examples - num_training_examples
        train_indices, val_indices = (
            permuted_indices[:num_training_examples],
            permuted_indices[num_training_examples:],
        )

        clipped_batch_size = min(batch_size, num_validation_examples)

        # num_atoms = theta.shape[0]
        clamp_and_warn("num_atoms", num_atoms, min_val=2, max_val=clipped_batch_size)

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(
            torch.cat(self._theta_bank), torch.cat(self._x_bank)
        )

        # Create neural net and validation loaders using a subset sampler.

        train_loader = data.DataLoader(
            dataset,
            batch_size=clipped_batch_size,
            drop_last=True,
            sampler=SubsetRandomSampler(train_indices),
        )
        val_loader = data.DataLoader(
            dataset,
            batch_size=clipped_batch_size,
            shuffle=False,
            drop_last=False,
            sampler=SubsetRandomSampler(val_indices),
        )

        optimizer = optim.Adam(
            list(self._posterior.net.parameters())
            + list(self._summary_net.parameters()),
            lr=learning_rate,
        )

        # If we're retraining from scratch each round, reset the neural posterior
        # to the untrained copy we made at the start.
        if self._retrain_from_scratch_each_round:
            self._posterior = deepcopy(self._posterior)

        epoch, self._val_log_prob = 0, float("-Inf")

        while epoch <= max_num_epochs and not self._converged(epoch, stop_after_epochs):

            # Train for a single epoch.
            self._posterior.net.train()
            for theta_batch, x_batch in train_loader:
                optimizer.zero_grad()
                loss = self._loss(theta_batch, x_batch, num_atoms)
                loss.backward()
                if clip_max_norm is not None:
                    clip_grad_norm_(
                        self._posterior.net.parameters(), max_norm=clip_max_norm,
                    )
                optimizer.step()

            epoch += 1

            # Calculate validation performance.
            self._posterior.net.eval()
            log_prob_sum = 0
            with torch.no_grad():
                for theta_batch, x_batch in val_loader:
                    log_prob = self._loss(theta_batch, x_batch, num_atoms)
                    log_prob_sum -= log_prob.sum().item()
                self._val_log_prob = log_prob_sum / num_validation_examples

            self._maybe_show_progress(self._show_progressbar, epoch)

        self._report_convergence_at_end(epoch, stop_after_epochs, max_num_epochs)

        # Update summary.
        self._summary["epochs"].append(epoch)
        self._summary["best_validation_log_probs"].append(self._best_val_log_prob)

    def _classifier_logits(self, theta: Tensor, x: Tensor, num_atoms: int) -> Tensor:
        """Return logits obtained through classifier forward pass.

        The logits are obtained from atomic sets of (theta,x) pairs.
        """
        batch_size = theta.shape[0]
        repeated_x = utils.repeat_rows(x, num_atoms)

        # Choose `1` or `num_atoms - 1` thetas from the rest of the batch for each x.
        probs = ones(batch_size, batch_size) * (1 - eye(batch_size)) / (batch_size - 1)

        choices = torch.multinomial(probs, num_samples=num_atoms - 1, replacement=False)

        contrasting_theta = theta[choices]

        atomic_theta = torch.cat((theta[:, None, :], contrasting_theta), dim=1).reshape(
            batch_size * num_atoms, -1
        )

        theta_and_x = torch.cat((atomic_theta, repeated_x), dim=1)

        return self._posterior.net(theta_and_x)

    @abstractmethod
    def _loss(
        self, theta: Tensor, x: Tensor, clipped_batch_size: int, num_atoms: int
    ) -> Tensor:
        raise NotImplementedError


class PotentialFunctionProvider:
    """
    This class is initialized without arguments during the initialization of the
    Posterior class. When called, it specializes to the potential function appropriate
    to the requested `mcmc_method`.

    NOTE: Why use a class?
    ----------------------
    During inference, we use deepcopy to save untrained posteriors in memory. deepcopy
    uses pickle which can't serialize nested functions
    (https://stackoverflow.com/a/12022055).

    It is important to NOT initialize attributes upon instantiation, because we need the
     most current trained posterior neural net.

    Returns:
        Potential function for use by either numpy or pyro sampler
    """

    def __call__(
        self, prior, classifier: nn.Module, x: Tensor, mcmc_method: str,
    ) -> Callable:
        r"""Return potential function for posterior $p(\theta|x)$.

        Switch on numpy or pyro potential function based on `mcmc_method`.

        Args:
            prior: Prior distribution that can be evaluated.
            classifier: Binary classifier approximating the likelihood up to a constant.

            x: Conditioning variable for posterior $p(\theta|x)$.
            mcmc_method: One of `slice_np`, `slice`, `hmc` or `nuts`.

        Returns:
            Potential function for sampler.
        """

        self.classifier = classifier
        self.prior = prior
        self.x = x

        if mcmc_method in ("slice", "hmc", "nuts"):
            return self.pyro_potential
        else:
            return self.np_potential

    def np_potential(self, theta: np.array) -> ScalarFloat:
        """Return potential for Numpy slice sampler."

        Args:
            theta: Parameters $\theta$, batch dimension 1.

        Returns:
            Posterior log probability of theta.
        """
        theta = torch.as_tensor(theta, dtype=torch.float32)

        # Theta and x should have shape (1, dim).
        theta = ensure_theta_batched(theta)
        x = ensure_x_batched(self.x)

        log_ratio = self.classifier(torch.cat((theta, x), dim=1).reshape(1, -1))

        # Notice opposite sign to pyro potential.
        return log_ratio + self.prior.log_prob(theta)

    def pyro_potential(self, theta: Dict[str, Tensor]) -> Tensor:
        """Return potential for Pyro sampler.

        Args:
            theta: Parameters $\theta$. The tensor's shape will be
             (1, shape_of_single_theta) if running a single chain or just
             (shape_of_single_theta) for multiple chains.

        Returns:
            Potential $-(\log r(x_o, \theta) + \log p(\theta))$.
        """

        theta = next(iter(theta.values()))

        # Theta and x should have shape (1, dim).
        theta = ensure_theta_batched(theta)
        x = ensure_x_batched(self.x)

        log_ratio = self.classifier(torch.cat((theta, x), dim=1).reshape(1, -1))

        return -(log_ratio + self.prior.log_prob(theta))
