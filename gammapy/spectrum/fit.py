from __future__ import absolute_import, division, print_function, unicode_literals
import logging
import copy
import numpy as np
import astropy.units as u
from ..utils.scripts import make_path
from ..utils.fitting import fit_iminuit
from .. import stats
from .utils import CountsPredictor
from . import SpectrumObservationList, SpectrumObservation
from itertools import product


__all__ = [
    'SpectrumFit',
]

log = logging.getLogger(__name__)


class SpectrumFit(object):
    """Orchestrate a 1D counts spectrum fit.

    After running the :func:`~gammapy.spectrum.SpectrumFit.fit` and
    :func:`~gammapy.spectrum.SpectrumFit.est_errors` methods, the fit results
    are available in :func:`~gammapy.spectrum.SpectrumFit.result`. For usage
    examples see :ref:`spectral_fitting`

    Parameters
    ----------
    obs_list : `~gammapy.spectrum.SpectrumObservationList`, `~gammapy.spectrum.SpectrumObservation`
        Observation(s) to fit
    model : `~gammapy.spectrum.models.SpectralModel`
        Source model with initial parameter values. Should return counts if
        ``forward_folded`` is False and a flux otherwise
    stat : {'wstat', 'cash'}
        Fit statistic
    forward_folded : bool, default: True
        Fold ``model`` with the IRFs given in ``obs_list``
    fit_range : tuple of `~astropy.units.Quantity`
        The intersection between the fit range and the observation thresholds will be used.
        If you want to control which bins are taken into account in the fit for each
        observation, use :func:`~gammapy.spectrum.PHACountsSpectrum.quality`
    method : {'iminuit'}
        Optimization backend for the fit
    error_method : {'covar', 'conf', 'HESSE', 'MINOS'}
        Method of the error estimation depending on the backend.
        TODO: Not implemented yet. For now 'covar'/'HESSE' are used by default.
    """

    def __init__(self, obs_list, model, stat='wstat', forward_folded=True,
                 fit_range=None, method='iminuit', error_method=None):
        self.obs_list = obs_list
        self._model = model
        self.stat = stat
        self.forward_folded = forward_folded
        self.fit_range = fit_range
        self.method = method
        self.error_method = error_method

        self._predicted_counts = None
        self._statval = None

        self.covar_axis = None
        self.covariance = None
        self._result = None

        self._check_valid_fit()
        self._apply_fit_range()

    def __str__(self):
        ss = self.__class__.__name__
        ss += '\nSource model {}'.format(self._model.__class__.__name__)
        ss += '\nStat {}'.format(self.stat)
        ss += '\nForward Folded {}'.format(self.forward_folded)
        ss += '\nFit range {}'.format(self.fit_range)
        ss += '\nBackend {}'.format(self.method)
        ss += '\nError Method {}'.format(self.error_method)

        return ss

    @property
    def result(self):
        """Fit result

        The result is a list of length ``n``, where ``n`` ist the number of
        observations that participated in the fit. The best fit model is
        usually the same for all observations but the results differ in the
        fitted energy range, predicted counts, final fit statistic value
        etc.
        """
        return self._result

    @property
    def obs_list(self):
        """Observations participating in the fit"""
        return self._obs_list

    @obs_list.setter
    def obs_list(self, obs_list):
        if isinstance(obs_list, SpectrumObservation):
            obs_list = SpectrumObservationList([obs_list])

        self._obs_list = SpectrumObservationList(obs_list)

    @property
    def bins_in_fit_range(self):
        """Bins participating in the fit for each observation."""
        return self._bins_in_fit_range

    @property
    def predicted_counts(self):
        """Current value of predicted counts.

        For each observation a tuple to counts for the on and off region is
        returned.
        """
        return self._predicted_counts

    @property
    def statval(self):
        """Current value of statval.

        For each observation the statval per bin is returned.
        """
        return self._statval

    @property
    def fit_range(self):
        """Fit range."""
        return self._fit_range

    @fit_range.setter
    def fit_range(self, fit_range):
        self._fit_range = fit_range
        self._apply_fit_range()

    @property
    def true_fit_range(self):
        """True fit range for each observation.

        True fit range is the fit range set in the
        `~gammapy.spectrum.SpectrumFit` with observation threshold taken into
        account.
        """
        true_range = []
        for binrange, obs in zip(self.bins_in_fit_range, self.obs_list):
            idx = np.where(binrange)[0]
            if len(idx) == 0:
                true_range.append(None)
                continue
            e_min = obs.e_reco[idx[0]]
            e_max = obs.e_reco[idx[-1] + 1]
            fit_range = u.Quantity((e_min, e_max))
            true_range.append(fit_range)
        return true_range

    def _apply_fit_range(self):
        """Mark bins within desired fit range for each observation."""
        self._bins_in_fit_range = []
        for obs in self.obs_list:
            # Take into account fit range
            energy = obs.e_reco
            valid_range = np.zeros(energy.nbins)

            if self.fit_range is not None:

                precision = 1e-3  # to avoid floating round precision
                idx_lo = np.where(energy * (1 + precision) < self.fit_range[0])[0]
                valid_range[idx_lo] = 1

                idx_hi = np.where(energy[:-1] * (1 - precision) > self.fit_range[1])[0]
                if len(idx_hi) != 0:
                    idx_hi = np.insert(idx_hi, 0, idx_hi[0] - 1)
                valid_range[idx_hi] = 1

            # Take into account thresholds
            try:
                quality = obs.on_vector.quality
            except AttributeError:
                quality = np.zeros(obs.e_reco.nbins)

            intersection = np.logical_and(1 - quality, 1 - valid_range)

            self._bins_in_fit_range.append(intersection)

    def predict_counts(self):
        """Predict counts for all observations.

        The result is stored as ``predicted_counts`` attribute.
        """
        predicted_counts = []
        for obs in self.obs_list:
            mu_sig = self._predict_counts_helper(obs,
                                                 self._model,
                                                 self.forward_folded)
            predicted_counts.append(mu_sig)
        self._predicted_counts = predicted_counts

    def _predict_counts_helper(self, obs, model, forward_folded=True):
        """Predict counts for one observation.

        Parameters
        ----------
        obs : `~gammapy.spectrum.SpectrumObservation`
            Response functions
        model : `~gammapy.spectrum.SpectralModel`
            Source or background model
        forward_folded : bool, default: True
            Fold model with IRFs

        Returns
        ------
        predicted_counts: `np.array`
            Predicted counts for one observation
        """
        predictor = CountsPredictor(model=model)
        if forward_folded:
            predictor.aeff = obs.aeff
            predictor.edisp = obs.edisp
        else:
            predictor.e_true = obs.e_reco

        predictor.livetime = obs.livetime

        predictor.run()
        counts = predictor.npred.data.data

        # Check count unit (~unit of model amplitude)
        if counts.unit.is_equivalent(''):
            counts = counts.value
        else:
            raise ValueError('Predicted counts {}'.format(counts))

        # Apply AREASCAL column
        counts *= obs.on_vector.areascal

        return counts

    def calc_statval(self):
        """Calc statistic for all observations.

        The result is stored as attribute ``statval``, bin outside the fit
        range are set to 0.
        """
        statval = []
        for obs, npred in zip(self.obs_list, self.predicted_counts):
            on_stat = self._calc_statval_helper(obs, npred)
            statval.append(on_stat)
        self._statval = statval
        self._restrict_statval()

    def _calc_statval_helper(self, obs, prediction):
        """Calculate ``statval`` for one observation.

        Parameters
        ----------
        obs : `~gammapy.spectrum.SpectrumObservation`
            Measured counts
        prediction : tuple of `~numpy.ndarray`
            Predicted counts

        Returns
        ------
        statsval : tuple of `~numpy.ndarray`
            Statval 
        """
        stats_func = getattr(stats, self.stat)

        if self.stat == 'cash' or self.stat == 'cstat':
            on_stat = stats_func(n_on=obs.on_vector.data.data.value,
                                 mu_on=prediction)

        elif self.stat == 'wstat':
            kwargs = dict(n_on=obs.on_vector.data.data.value,
                          n_off=obs.off_vector.data.data.value,
                          alpha=obs.alpha,
                          mu_sig=prediction)
            on_stat_ = stats_func(**kwargs)
            on_stat = np.nan_to_num(on_stat_)
        else:
            raise NotImplementedError('{}'.format(self.stat))

        return on_stat

    def total_stat(self, parameters):
        """Statistic summed over all bins and all observations.

        This is the likelihood function that is passed to the optimizers

        Parameters
        ----------
        parameters : `~gammapy.utils.fitting.ParameterList`
            Model parameters
        """
        self._model.parameters = parameters
        self.predict_counts()
        self.calc_statval()
        total_stat = np.sum([np.sum(v) for v in self.statval], dtype=np.float64)
        return total_stat

    def _restrict_statval(self):
        """Apply valid fit range to statval.
        """
        for statval, valid_range in zip(self.statval, self.bins_in_fit_range):
            # Find bins outside safe range
            idx = np.where(np.invert(valid_range))[0]
            statval[idx] = 0

    def _check_valid_fit(self):
        """Helper function to give useful error messages."""
        # Assume that settings are the same for all observations
        test_obs = self.obs_list[0]
        irfs_exist = test_obs.aeff is not None or test_obs.edisp is not None
        if self.forward_folded and not irfs_exist:
            raise ValueError('IRFs required for forward folded fit')
        if self.stat == 'wstat' and self.obs_list[0].off_vector is None:
            raise ValueError('Off vector required for WStat fit')
        try:
            test_obs.livetime
        except KeyError:
            raise ValueError('No observation livetime given')

    def likelihood_1d(self, model, parname, parvals):
        """Compute likelihood profile.

        Parameters
        ----------
        model : `~gammapy.spectrum.models.SpectralModel`
            Model to draw likelihood profile for
        parname : str
            Parameter to calculate profile for
        parvals : `~astropy.units.Quantity`
            Parameter values
        """
        likelihood = []
        self._model = model
        for val in parvals:
            self._model.parameters[parname].value = val
            stat = self.total_stat(self._model.parameters)
            likelihood.append(stat)
        return np.array(likelihood)

    def plot_likelihood_1d(self, ax=None, **kwargs):
        """Plot 1-dim likelihood profile.

        See :func:`~gammapy.spectrum.SpectrumFit.likelihood_1d`
        """
        import matplotlib.pyplot as plt
        ax = plt.gca() if ax is None else ax

        yy = self.likelihood_1d(**kwargs)
        ax.plot(kwargs['parvals'], yy)
        ax.set_xlabel(kwargs['parname'])

        return ax

    def fit(self, opts_minuit=None):
        """Run the fit

        Parameters
        ----------
        opts_minuit : dict (optional)
            Options passed to `iminuit.Minuit` constructor
        """
        if self.method == 'iminuit':
            self._fit_iminuit(opts_minuit)
        else:
            raise NotImplementedError('method: {}'.format(self.method))

    def _fit_iminuit(self, opts_minuit):
        """Iminuit minimization"""
        minuit = fit_iminuit(parameters=self._model.parameters,
                             function=self.total_stat,
                             opts_minuit=opts_minuit)
        self._iminuit_fit = minuit
        log.debug(minuit)
        self._make_fit_result(self._model.parameters)

    def _make_fit_result(self, parameters):
        """Bundle fit results into `~gammapy.spectrum.SpectrumFitResult`.

        Parameters
        ----------
        parameters : `~gammapy.utils.modeling.ParameterList`
            Best fit parameters
        """
        from . import SpectrumFitResult

        # run again with best fit parameters
        self.total_stat(parameters)
        model = self._model.copy()

        statname = self.stat

        results = []
        for idx, obs in enumerate(self.obs_list):
            fit_range = self.true_fit_range[idx]
            statval = np.sum(self.statval[idx])
            stat_per_bin = self.statval[idx]
            npred_src = copy.deepcopy(self.predicted_counts[idx])

            results.append(SpectrumFitResult(
                model=model,
                fit_range=fit_range,
                statname=statname,
                statval=statval,
                stat_per_bin=stat_per_bin,
                npred_src=npred_src,
                obs=obs
            ))

        self._result = results

    def est_errors(self):
        """Estimate parameter errors."""
        if self.method == 'iminuit':
            self._est_errors_iminuit()
        else:
            raise NotImplementedError('{}'.format(self.method))

        for res in self.result:
            res.covar_axis = self.covar_axis
            res.covariance = self.covariance
            res.model.parameters.set_parameter_covariance(self.covariance, self.covar_axis)

    def _est_errors_iminuit(self):
        # The iminuit covariance is a dict indexed by tuples containing combinations of
        # parameter names

        # create tuples of combinations
        d = self._model.parameters.to_dict()
        parameter_names = [l['name'] for l in d['parameters'] if not l['frozen']]
        self.covar_axis = parameter_names
        parameter_combinations = list(product(parameter_names, repeat=2))

        if self._iminuit_fit.covariance:
            iminuit_covariance = self._iminuit_fit.covariance
            cov = np.array([iminuit_covariance[c] for c in parameter_combinations])
        else:
            # fit did not converge
            cov = np.repeat(np.nan, len(parameter_combinations))

        cov = cov.reshape(len(parameter_names), -1)
        self.covariance = cov

    def run(self, outdir=None):
        """Run all steps and write result to disk.

        Parameters
        ----------
        outdir : Path, str
            directory to write results files to (if given)
        """
        log.info('Running {}'.format(self))

        self.fit()
        self.est_errors()

        if outdir is not None:
            self._write_result(outdir)

    def _write_result(self, outdir):
        outdir = make_path(outdir)
        outdir.mkdir(exist_ok=True, parents=True)

        # Assume only one model is fit to all data
        modelname = self.result[0].model.__class__.__name__
        filename = outdir / 'fit_result_{}.yaml'.format(modelname)
        log.info('Writing {}'.format(filename))
        self.result[0].to_yaml(filename)
