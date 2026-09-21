"""Cited numerical methods shared by Transport analysis and its portable replay."""
import math
import statistics

import numpy as np
from scipy.stats import t

import transport


METHOD_VERSION = '2.0.0'
REFERENCES = {
    'nist-ols': {
        'title': 'NIST/SEMATECH e-Handbook: Linear Least Squares Regression',
        'author': 'NIST/SEMATECH', 'section': '4.1.4.1 and 4.4.3.1',
        'url': 'https://www.itl.nist.gov/div898/handbook/pmd/section1/pmd141.htm',
    },
    'gum': {
        'title': 'Evaluation of measurement data: Guide to the expression of uncertainty in measurement',
        'author': 'Joint Committee for Guides in Metrology', 'year': '2008',
        'section': 'Sections 5 (combined standard uncertainty) and 7 (reporting)',
        'doi': '10.59161/JCGM100-2008E', 'url': 'https://doi.org/10.59161/JCGM100-2008E',
    },
    'scipy-t': {
        'title': 'SciPy: Student t distribution', 'author': 'SciPy developers',
        'section': 'scipy.stats.t.ppf',
        'url': 'https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.t.html',
    },
    'numpy-trapezoid': {
        'title': 'NumPy: composite trapezoidal integration', 'author': 'NumPy developers',
        'section': 'numpy.trapezoid',
        'url': 'https://numpy.org/doc/stable/reference/generated/numpy.trapezoid.html',
    },
    'tek-gm': {
        'title': 'How do you find the transconductance of a MOSFET?', 'author': 'Tektronix',
        'section': 'Measuring transconductance at constant drain-source voltage',
        'url': 'https://www.tek.com/en/support/faqs/how-do-you-find-the-transconductance-of-a-mosfet',
    },
    'nist-resistivity': {
        'title': 'Resistivity and Hall Measurements', 'author': 'NIST',
        'section': 'Sample geometry and resistivity calculations; geometry must match the model',
        'url': 'https://www.nist.gov/pml/nanoscale-device-characterization-division/popular-links/hall-effect/resistivity-and-hall',
    },
    'openstax-resistance': {
        'title': 'University Physics Volume 2: Resistivity and Resistance', 'author': 'OpenStax',
        'section': '9.3, resistance of a uniform conductor: R = rho L / A',
        'url': 'https://openstax.org/books/university-physics-volume-2/pages/9-3-resistivity-and-resistance',
    },
}


def cited(ids):
    return [{'id': key, **REFERENCES[key]} for key in dict.fromkeys(ids)]


def linear_fit(points):
    """Unweighted I = slope*x + intercept with a free intercept and fit-only intervals."""
    if len(points) < 3:
        return None
    x, y = np.asarray(points, dtype=float).T
    fit = transport._linear_fit(x.tolist(), y.tolist())
    if fit is None:
        return None
    predicted = fit['intercept'] + fit['slope'] * x
    residual = y - predicted
    x_mean = statistics.fmean(x.tolist())
    y_mean = statistics.fmean(y.tolist())
    sxx = sum((value - x_mean) ** 2 for value in x.tolist())
    sxy = sum((a - x_mean) * (b - y_mean) for a, b in zip(x.tolist(), y.tolist()))
    sse = float(np.sum(residual ** 2))
    dof = len(x) - 2
    variance = sse / dof
    se = math.sqrt(variance / sxx)
    intercept_se = math.sqrt(variance * (1 / len(x) + x_mean ** 2 / sxx))
    critical = float(t.ppf(.975, dof))
    fit.update(
        method='ordinary_least_squares', implementation='transport_methods.linear_fit',
        method_version=METHOD_VERSION, equation='I = slope*x + intercept',
        weights='equal', intercept_policy='free', degrees_of_freedom=dof,
        slope_standard_error=se, intercept_standard_error=intercept_se,
        slope_intercept_covariance=-x_mean * variance / sxx,
        confidence_level=.95, t_critical=critical,
        slope_ci95=[fit['slope'] - critical * se, fit['slope'] + critical * se],
        intercept_ci95=[fit['intercept'] - critical * intercept_se, fit['intercept'] + critical * intercept_se],
        residual_sum_squares=sse, residual_standard_error=math.sqrt(variance),
        rmse=math.sqrt(sse / len(x)), x_mean=x_mean, y_mean=y_mean,
        sxx=sxx, sxy=sxy,
        x_min=float(x.min()), x_max=float(x.max()),
        fitted_values=predicted.tolist(), residuals=residual.tolist(),
        uncertainty_scope='Fit scatter only. Independent, constant-variance errors in current and negligible x error are assumed. Student-t intervals additionally assume normal errors.',
        reference_ids=['nist-ols', 'gum', 'scipy-t'],
    )
    if not all(math.isfinite(value) for value in [fit['slope'], fit['intercept'], se, intercept_se, sse]):
        raise ValueError('The fit exceeds finite numerical precision. Review channel scales and data range.')
    return fit


def interpret_fit(fit, channel, axis, options):
    """Name the measured response before deriving geometry-dependent quantities."""
    if fit is None:
        return
    own_voltage = axis == channel['voltage']
    transfer = not own_voltage and channel.get('terminal_role') == 'Drain' and axis.casefold() in {'gatev', 'vgs'}
    fit['response_kind'] = 'conductance' if own_voltage else 'transfer_slope' if transfer else 'cross_channel_response'
    fit['slope_unit'] = 'A/V'
    fit['resistance_ohm'] = None
    fit['resistance_standard_uncertainty_ohm'] = None
    fit['resistance_ci95_ohm'] = None
    fit['resistivity_ohm_m'] = None
    fit['conductivity_s_m'] = None
    fit['physical_status'] = 'assumption-dependent'
    fit['missing_inputs'] = []
    if not options.get('units_confirmed'):
        fit['missing_inputs'].append('Confirm the current and voltage units and scales.')
    if not options.get('wiring_confirmed'):
        fit['missing_inputs'].append('Confirm the terminal roles, voltage reference and wiring.')
    if channel.get('voltage_status', 'unknown') != 'measured':
        fit['missing_inputs'].append('Confirm measured device voltage; programmed or unknown voltage can include lead/contact drops.')
    if transfer:
        fit['reference_ids'].append('tek-gm')
        fit['interpretation'] = 'Drain-current response to gate voltage. Interpret as transconductance only with fixed drain-source voltage and a confirmed source reference.'
        fit['missing_inputs'].append('Confirm fixed drain-source voltage and the gate-source voltage reference.')
    elif not own_voltage:
        fit['interpretation'] = 'Response of one current channel to another terminal voltage. Resistance is unavailable for this cross-channel slope.'
    else:
        fit['interpretation'] = 'Reciprocal slope describes resistance over this fit interval, conditional on the voltage reference and an approximately linear response. Contact and lead contributions depend on the wiring.'
        low, high = fit['slope_ci95']
        slope = fit['slope']
        if slope and not low <= 0 <= high:
            fit['resistance_ohm'] = 1 / slope
            fit['resistance_standard_uncertainty_ohm'] = fit['slope_standard_error'] / slope ** 2
            fit['resistance_ci95_ohm'] = sorted([1 / low, 1 / high])
        else:
            fit['missing_inputs'].append('Resistance is unbounded because the slope interval includes zero.')
        if not fit['missing_inputs']:
            fit['physical_status'] = 'calculated'
        geometry = options.get('geometry_model', 'none')
        if geometry == 'rectangular_bar':
            fit['reference_ids'].append('openstax-resistance')
            required = ['length_m', 'width_m', 'thickness_m']
            absent = [name for name in required if not options.get(name)]
            if absent:
                fit['missing_inputs'].append('Resistivity requires ' + ', '.join(absent) + '.')
            elif not options.get('wiring_confirmed') or not options.get('units_confirmed') or options.get('measurement_configuration') != 'four_terminal' or channel.get('voltage_status') != 'measured':
                fit['missing_inputs'].append('Bulk resistivity requires confirmed units, measured four-terminal voltage, and a uniform rectangular-bar geometry.')
            elif fit['resistance_ohm'] is None or fit['resistance_ohm'] <= 0:
                fit['missing_inputs'].append('Positive, bounded resistance is required for this bulk resistivity model.')
            else:
                resistance = fit['resistance_ohm']
                rho = resistance * options['width_m'] * options['thickness_m'] / options['length_m']
                fit['resistivity_ohm_m'] = rho
                fit['conductivity_s_m'] = 1 / rho
                uncertainty_keys = ['current_relative_standard_uncertainty', 'voltage_relative_standard_uncertainty',
                                    'length_standard_uncertainty_m', 'width_standard_uncertainty_m', 'thickness_standard_uncertainty_m']
                if all(options.get(key) is not None for key in uncertainty_keys):
                    terms = [(fit['resistance_standard_uncertainty_ohm'] / resistance) ** 2,
                             options[uncertainty_keys[0]] ** 2, options[uncertainty_keys[1]] ** 2]
                    terms.extend((options[f'{name}_standard_uncertainty_m'] / options[f'{name}_m']) ** 2 for name in ['length', 'width', 'thickness'])
                    fit['resistivity_standard_uncertainty_ohm_m'] = rho * math.sqrt(sum(terms))
                    fit['conductivity_standard_uncertainty_s_m'] = fit['resistivity_standard_uncertainty_ohm_m'] / rho ** 2
                else:
                    fit['missing_inputs'].append('Combined resistivity uncertainty requires current, voltage and all geometry standard uncertainties; independence is assumed.')


def window_statistics(points, rows):
    """Do not bridge gaps or reversed/repeated times when integrating current."""
    contiguous = all(b == a + 1 for a, b in zip(rows, rows[1:]))
    increasing = all(b[0] > a[0] for a, b in zip(points, points[1:]))
    enough = len(points) >= 2
    integrate = enough and contiguous and increasing
    return {
        'source_rows': rows,
        'current_statistics_a': transport._statistics([p[1] for p in points]),
        'drift_a_per_s': linear_fit(points),
        'integrated_signed_current_c': float(np.trapezoid([p[1] for p in points], [p[0] for p in points])) if integrate else None,
        'integration_reason': None if integrate else 'Integration requires at least two adjacent observations with strictly increasing times; gaps are not interpolated.',
        'integration_equation': 'Q = sum((I[j] + I[j+1]) / 2 * (t[j+1] - t[j]))',
        'reference_ids': (['numpy-trapezoid'] if integrate else []) + (['nist-ols', 'gum', 'scipy-t'] if len(points) >= 3 and len({p[0] for p in points}) > 1 else []),
        'uncertainty_scope': 'Window standard deviation describes the observed population. Integrated-current measurement uncertainty is unavailable without current and timing error models.',
    }
