"""Module containing the :class:`Pulse` class and functions for initializing
pulse shapes."""
import logging
import re
from collections.abc import MutableMapping

import matplotlib.pyplot as plt
import numpy as np
import scipy.fftpack
from matplotlib.gridspec import GridSpec
from numpy.fft import fft, fftfreq
from scipy import signal
from scipy.interpolate import UnivariateSpline

from .io import open_file, writetotxt
from .linalg import iscomplexobj
from .units import UnitConvert, UnitFloat


class _PulseConfigAttribs(MutableMapping):
    """Custom ordered dict of config file attributes of pulses.

    The 'type' key is fixed to the value 'file', and the keys listed in
    `synchronized_keys` are linked to the corresponding attribute of
    the parent pulse. Furthermore, the value of the 'is_complex' key is linked
    to the type of the amplitude attribute of the parent pulse.

    Args:
        parent (Pulse): The pulse to which the settings apply
    """

    _synchronized_keys = ['time_unit', 'ampl_unit']
    _read_only_keys = ['type', 'is_complex']
    _required_keys = [
        'id',
        'type',
        'filename',
        'time_unit',
        'ampl_unit',
        'is_complex',
    ]

    def __init__(self, parent):
        self._parent = parent
        self._keys = list(self._required_keys)  # copy
        # the 'filename' and 'id' values are set to an "invalid" value on
        # purpose: if written to config file without overriding them, Fortran
        # will complain
        self._data = {'id': -1, 'type': 'file', 'filename': ''}

    def __setitem__(self, key, value):
        if key in self._read_only_keys:
            if value != self[key]:
                # Not allowing to reset read-only-keys to their same value
                # would make serialization difficult
                raise ValueError("'%s' setting is read-only" % key)
        elif key in self._synchronized_keys:
            if value != self[key]:
                setattr(self._parent, key, value)
        else:
            if key not in self._data:
                self._keys.append(key)
            self._data[key] = value

    def __getitem__(self, key):
        if key == 'is_complex':
            return self._parent.is_complex
        elif key in self._synchronized_keys:
            return getattr(self._parent, key)
        else:
            return self._data[key]

    def __delitem__(self, key):
        if key in self._required_keys:
            raise ValueError("Cannot delete key %s" % key)
        else:
            del self._data[key]
            self._keys.remove(key)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def __str__(self):
        items = ["(%r, %r)" % (key, val) for (key, val) in self.items()]
        return "[%s]" % (", ".join(items))

    def copy(self):
        """Shallow copy of object"""
        c = _PulseConfigAttribs(self._parent)
        c._data = self._data.copy()
        c._keys = list(self._keys)
        return c

    def __copy__(self):
        return self.copy()


class Pulse:
    """Numerical real or complex control pulse

    Args:
        tgrid (numpy.ndarray(float)):
            Time grid values
        amplitude (numpy.ndarray(float), numpy.ndarray(complex)):
            Amplitude values. If not given, amplitude will be zero
        time_unit (str): Unit of values in `tgrid`. Will be ignored when
            reading from file.
        ampl_unit (str): Unit of values in `amplitude`. Will be ignored when
            reading from file.
        freq_unit (str): Unit of frequencies when calculating spectrum. If not
            given, an appropriate unit based on `time_unit` will be chosen, if
            possible (or a `TypeError` will be raised.

    Attributes:
        tgrid (numpy.ndarray(float)): time points at which the pulse values
            are defined, from ``t0 + dt/2`` to ``T - dt/2``.
        amplitude (numpy.ndarray(float), numpy.ndarray(complex)): array
            of real or complex pulse values.
        time_unit (str): Unit of values in `tgrid`
        ampl_unit (str): Unit of values in `amplitude`
        freq_unit (str): Unit to use for frequency when calculating the
            spectrum
        dt (float): Time step (in `time_unit`)
        preamble (list): List of lines that are written before the header
            when writing the pulse to file. Each line should start with '# '
        postamble (list): List of lines that are written after all data
            lines. Each line should start with '# '
        config_attribs (dict): Additional config data, for when generating a
            QDYN config file section describing the pulse (e.g.
            `{'oct_shape': 'flattop', 't_rise': '10_ns'}`)

    Class Attributes:
        unit_convert (QDYN.units.UnitConvert): converter to be used for any
            unit conversion within any methods

    Example:

        >>> tgrid = pulse_tgrid(10, 100)
        >>> amplitude = 100 * gaussian(tgrid, 50, 10)
        >>> p = Pulse(tgrid=tgrid, amplitude=amplitude, time_unit='ns',
        ...           ampl_unit='MHz')
        >>> p.write('pulse.dat')
        >>> p2 = Pulse.read('pulse.dat')
        >>> from os import unlink; unlink('pulse.dat')

    Notes:

        It is important to remember that the QDYN Fortran library considers
        pulses to be defined on the intervals of the propagation time grid
        (i.e. for a time grid with n time steps of dt, the pulse will have n-1
        points, defined at points shifted by dt/2)

        The `pulse_tgrid` and `tgrid_from_config` routine may be used to obtain
        the proper pulse time grid from the propagation time grid::

            >>> import numpy as np
            >>> p = Pulse(tgrid=pulse_tgrid(10, 100), ampl_unit='MHz',
            ...           time_unit='ns')
            >>> len(p.tgrid)
            99
            >>> print(str(p.dt))
            0.10101_ns
            >>> p.t0
            0
            >>> print("%.5f" % p.tgrid[0])
            0.05051
            >>> print(str(p.T))
            10_ns
            >>> print("%.5f" % p.tgrid[-1])
            9.94949

        The type of the `amplitude` (not whether there is a non-zero
        imaginary part) decide whether the pulse is considered real or complex.
        Complex pulses are not allowed to couple to Hermitian operators, and
        in an optimization, both the real and imaginary part of the pulse are
        modified.
    """

    unit_convert = UnitConvert()

    freq_units = {  # map time_unit to most suitable freq_unit
        'ns': 'GHz',
        'ps': 'cminv',
        'fs': 'eV',
        'microsec': 'MHz',
        'au': 'au',
        'iu': 'iu',
        'unitless': 'unitless',
        'dimensionless': 'dimensionless',
    }

    def __init__(
        self,
        tgrid,
        amplitude=None,
        time_unit=None,
        ampl_unit=None,
        freq_unit=None,
        config_attribs=None,
    ):
        tgrid = np.array(tgrid, dtype=np.float64)
        if amplitude is None:
            amplitude = np.zeros(len(tgrid))
        if iscomplexobj(amplitude):
            amplitude = np.array(amplitude, dtype=np.complex128)
        else:
            amplitude = np.array(amplitude, dtype=np.float64)
        self.tgrid = tgrid
        self.amplitude = amplitude
        if time_unit is None:
            raise TypeError("time_unit must be given as a string")
        else:
            self.time_unit = time_unit
        if ampl_unit is None:
            raise TypeError("ampl_unit must be given as a string")
        else:
            self.ampl_unit = ampl_unit

        self.preamble = []
        self.postamble = []

        self.freq_unit = freq_unit
        if freq_unit is None:
            try:
                self.freq_unit = self.freq_units[self.time_unit]
            except KeyError:
                raise TypeError("freq_unit must be specified")
        self.config_attribs = _PulseConfigAttribs(self)
        if config_attribs is not None:
            for key in config_attribs:
                self.config_attribs[key] = config_attribs[key]
        self._check()

    def __eq__(self, other):
        """Compare two pulses, within a precision of 1e-12"""
        if not isinstance(other, self.__class__):
            return False
        public_attribs = [
            'is_complex',
            'time_unit',
            'ampl_unit',
            'freq_unit',
            'preamble',
            'postamble',
            'config_attribs',
        ]
        for attr in public_attribs:
            if getattr(self, attr) != getattr(other, attr):
                return False
        try:
            if np.max(np.abs(self.tgrid - other.tgrid)) > 1.0e-12:
                return False
            if np.max(np.abs(self.amplitude - other.amplitude)) > 1.0e-12:
                return False
        except ValueError:
            return False
        return True

    def copy(self):
        """Return a copy of the pulse"""
        return self.__class__(
            self.tgrid,
            self.amplitude,
            time_unit=self.time_unit,
            ampl_unit=self.ampl_unit,
            freq_unit=self.freq_unit,
            config_attribs=self.config_attribs,
        )

    def __copy__(self):
        return self.copy()

    def _check(self):
        """Assert self-consistency of pulse"""
        assert self.tgrid is not None, "Pulse is not initialized"
        assert self.amplitude is not None, "Pulse is not initialized"
        assert isinstance(self.tgrid, np.ndarray), "tgrid must be numpy array"
        assert isinstance(
            self.amplitude, np.ndarray
        ), "amplitude must be numpy array"
        assert (
            self.tgrid.dtype.type is np.float64
        ), "tgrid must be double precision"
        assert self.amplitude.dtype.type in [
            np.float64,
            np.complex128,
        ], "amplitude must be double precision"
        assert len(self.tgrid) == len(
            self.amplitude
        ), "length of tgrid and amplitudes do not match"
        assert self.ampl_unit in self.unit_convert.units, (
            "Unknown ampl_unit %s" % self.ampl_unit
        )
        assert self.time_unit in self.unit_convert.units, (
            "Unknown time_unit %s" % self.time_unit
        )
        assert self.freq_unit in self.unit_convert.units, (
            "Unknown freq_unit %s" % self.freq_unit
        )

    @classmethod
    def read(
        cls,
        filename,
        time_unit=None,
        ampl_unit=None,
        freq_unit=None,
        ignore_header=False,
    ):
        """Read a pulse from file, in the format generated by the QDYN
        ``write_pulse`` routine.

        Parameters:

            filename (str): Path and name of file from which to read the pulse
            time_unit (str or None): The unit of the time grid
            ampl_unit (str or None): The unit of the pulse amplitude.
            freq_unit (str or None): Intended value for the `freq_unit`
                attribute. If None, a `freq_unit` will be chosen automatically,
                if possible (or a `TypeError` will be raised)
            ignore_header (bool): If True, the file header will be ignored.

        Note:

            By default, the file is assumed to contain a header that
            identifies the columns and their units, as a comment line
            immediately preceding the data. If `time_unit` or `ampl_unit` are
            None, and `ignore_header` is False, the respective unites are
            extracted from the header line. If `time_unit` or `ampl_unit` are
            not None, the respective values will be converted from the unit
            specified in the file header. If `ignore_header` is True, both
            `time_unit` and `ampl_unit` must be given. This can be used to read
            in pulses that were not generated by the QDYN ``write_pulse``
            routine.  Note that if `ignore_header` is True, *all* comment lines
            preceding the data will be included in the `preamble` attribute.

            The `write` method allows to restore *exactly* the original pulse
            file.
        """
        logger = logging.getLogger(__name__)
        header_rx = {
            'complex': re.compile(
                r'''
                ^\#\s*t(ime)? \s* \[\s*(?P<time_unit>\w+)\s*\]\s*
                Re\((ampl|E)\) \s* \[\s*(?P<ampl_unit>\w+)\s*\]\s*
                Im\((ampl|E)\) \s* \[(\w+)\]\s*$''',
                re.X | re.I,
            ),
            'real': re.compile(
                r'''
                ^\#\s*t(ime)? \s* \[\s*(?P<time_unit>\w+)\s*\]\s*
                Re\((ampl|E)\) \s* \[\s*(?P<ampl_unit>\w+)\s*\]\s*$''',
                re.X | re.I,
            ),
            'abs': re.compile(
                r'''
                ^\#\s*t(ime)? \s* \[\s*(?P<time_unit>\w+)\s*\]\s*
                (Abs\()?(ampl|E)(\))? \s* \[\s*(?P<ampl_unit>\w+)\s*\]\s*$''',
                re.X | re.I,
            ),
        }

        try:
            t, x, y = np.genfromtxt(filename, unpack=True, dtype=np.float64)
        except ValueError:
            t, x = np.genfromtxt(filename, unpack=True, dtype=np.float64)
            y = None
        preamble = []
        postamble = []
        with open_file(filename) as in_fh:
            in_preamble = True
            for line in in_fh:
                if line.startswith('#'):
                    if in_preamble:
                        preamble.append(line.strip())
                    else:
                        postamble.append(line.strip())
                else:
                    if in_preamble:
                        in_preamble = False
            # the last line of the preamble *must* be the header line. We will
            # process it and remove it from preamble
            mode = None
            file_time_unit = None
            file_ampl_unit = None
            if ignore_header:
                mode = 'complex'
                if y is None:
                    mode = 'real'
            else:
                try:
                    header_line = preamble.pop()
                except IndexError:
                    raise IOError("Pulse file does not contain a preamble")
                for file_mode, pattern in header_rx.items():
                    match = pattern.match(header_line)
                    if match:
                        mode = file_mode
                        file_time_unit = match.group('time_unit')
                        file_ampl_unit = match.group('ampl_unit')
                        break
                if mode is None:
                    logger.warning(
                        "Non-standard header in pulse file."
                        "Check that pulse was read with correct units"
                    )
                    if y is None:
                        mode = 'real'
                    else:
                        mode = 'complex'
                    free_pattern = re.compile(
                        r'''
                    ^\# .* \[\s*(?P<time_unit>\w+)\s*\]
                        .* \[\s*(?P<ampl_unit>\w+)\s*\]''',
                        re.X,
                    )
                    match = free_pattern.search(header_line)
                    if match:
                        file_time_unit = match.group('time_unit')
                        file_ampl_unit = match.group('ampl_unit')
                        logger.info("Identify time_unit = %s", file_time_unit)
                        logger.info("Identify ampl_unit = %s", file_ampl_unit)
                if file_time_unit is None or file_ampl_unit is None:
                    raise ValueError("Could not identify units from header")

        if mode == 'abs':
            amplitude = x
        elif mode == 'real':
            amplitude = x
        elif mode == 'complex':
            amplitude = x + 1j * y
        else:
            raise ValueError("mode must be 'abs', 'real', or 'complex'")

        if not ignore_header:
            if time_unit is None:
                time_unit = file_time_unit
            else:
                t = cls.unit_convert.convert(t, file_time_unit, time_unit)
            if ampl_unit is None:
                ampl_unit = file_ampl_unit
            else:
                amplitude = cls.unit_convert.convert(
                    amplitude, file_ampl_unit, ampl_unit
                )

        pulse = cls(
            tgrid=t,
            amplitude=amplitude,
            time_unit=time_unit,
            ampl_unit=ampl_unit,
            freq_unit=freq_unit,
        )
        pulse.preamble = preamble
        pulse.postamble = postamble
        return pulse

    @classmethod
    def from_func(
        cls,
        tgrid,
        func,
        time_unit=None,
        ampl_unit=None,
        freq_unit=None,
        config_attribs=None,
    ):
        """Instantiate a pulse from an amplitude function `func`.

        All other parameters are passed on to `__init__`
        """
        amplitude = [func(t) for t in tgrid]
        return cls(
            tgrid,
            amplitude=amplitude,
            time_unit=time_unit,
            ampl_unit=ampl_unit,
            freq_unit=freq_unit,
            config_attribs=config_attribs,
        )

    @property
    def dt(self):
        """Time grid step, as instance of `UnitFloat`"""
        return UnitFloat(self.tgrid[1] - self.tgrid[0], unit=self.time_unit)

    @property
    def t0(self):
        """Time at which the pulse begins (dt/2 before the first point in the
        pulse), as instance of `UnitFloat`
        """
        result = self.tgrid[0] - 0.5 * float(self.dt)
        if abs(result) < 1.0e-15 * self.tgrid[-1]:
            result = 0.0
        return UnitFloat(result, self.time_unit)

    @property
    def states_tgrid(self):
        """Time grid values for the states propagated under the numerical pulse
        values, as numpy array in units of :attr:`time_unit`.

        The returned time grid has one point more than :attr:`tgrid`, and
        extends from :attr:`t0` to :attr:`T` (inclusive).
        """
        return np.linspace(float(self.t0), float(self.T), len(self.tgrid) + 1)

    @property
    def w_max(self):
        """Maximum frequency that can be represented with the
        current sampling rate.
        """
        n = len(self.tgrid)
        dt = float(self.unit_convert.convert(self.dt, self.time_unit, 'iu'))
        if n % 2 == 1:
            # odd
            w_max = ((n - 1) * np.pi) / (n * dt)
        else:
            # even
            w_max = np.pi / dt
        return self.unit_convert.convert(w_max, 'iu', self.freq_unit)

    @property
    def dw(self):
        """Step width in the spectrum (i.e. the spectral resolution)
        based on the current pulse duration, as an instance of
        :class:`~qdyn.units.UnitFloat`.
        """
        n = len(self.tgrid)
        w_max = self.w_max
        if n % 2 == 1:
            # odd
            return 2.0 * w_max / float(n - 1)
        else:
            # even
            return 2.0 * w_max / float(n)

    @property
    def T(self):
        """Time at which the pulse ends (dt/2 after the last point in the
        pulse), as an instance of :class:`~qdyn.units.UnitFloat`.
        """
        result = self.tgrid[-1] + 0.5 * float(self.dt)
        if abs(round(result) - result) < (1.0e-15 * result):
            result = round(result)
        return UnitFloat(result, unit=self.time_unit)

    @property
    def is_complex(self):
        """Is the pulse amplitude of complex type?"""
        return iscomplexobj(self.amplitude)

    def as_func(self, interpolation='linear', allow_args=False):
        """Callable that evaluates the pulse for a given time value.

        Possible values for `interpolation` are 'linear' and 'piecewise'.

        The resulting function takes an argument `t` that must be a float
        in the range [:attr:`t0`, :attr:`T`] and in units of
        :attr:`time_unit`). It returns the
        (interpolated) pulse amplitude as a float, in units of
        :attr:`ampl_unit`

        If `allow_args` is True, the resulting function takes a second argument
        `args` that is ignored. This is for compatibility with qutip, see
        http://qutip.org/docs/latest/guide/dynamics/dynamics-time.html.
        """

        t0 = float(self.t0)
        T = float(self.T)
        dt = float(self.dt)
        offset = t0 + 0.5 * dt

        def func_linear(t):
            """linear interpolation of pulse amplitude"""
            if t0 <= float(t) <= T:
                t = float(t) - offset
                n = max(int(t / dt), 0)
                delta = max(t - n * dt, 0.0) / dt
                try:
                    return (1 - delta) * self.amplitude[
                        n
                    ] + delta * self.amplitude[n + 1]
                except IndexError:  # last n
                    return self.amplitude[n]
            else:
                raise ValueError(
                    "Value t=%g not in range [%g, %g]" % (t, t0, T)
                )

        def func_piecewise(t):
            """piecewise interpolation of pulse amplitude"""
            if t0 <= float(t) <= T:
                t = float(t) - offset
                n = max(int(t / dt), 0)
                delta = max(t - n * dt, 0.0) / dt
                if delta < 0.5:
                    return self.amplitude[n]
                else:
                    try:
                        return self.amplitude[n + 1]
                    except IndexError:  # last n
                        return self.amplitude[n]
            else:
                raise ValueError(
                    "Value t=%g not in range [%g, %g]" % (t, t0, T)
                )

        def _attach_args(func):
            def func_with_args(t, args):
                return func(t)

            return func_with_args

        func_map = {'linear': func_linear, 'piecewise': func_piecewise}
        try:
            if allow_args:
                return _attach_args(func_map[interpolation])
            else:
                return func_map[interpolation]
        except KeyError:
            raise ValueError(
                "Invalid interpolation not in %s: %s"
                % (str(list(func_map.keys())), interpolation)
            )

    def convert(self, time_unit=None, ampl_unit=None, freq_unit=None):
        """Convert the pulse data to different units"""
        if time_unit is not None:
            factor = self.unit_convert.convert(1.0, self.time_unit, time_unit)
            self.tgrid *= factor
            self.time_unit = time_unit
        if ampl_unit is not None:
            factor = self.unit_convert.convert(1.0, self.ampl_unit, ampl_unit)
            self.amplitude *= factor
            self.ampl_unit = ampl_unit
        if freq_unit is not None:
            self.freq_unit = freq_unit
        self._check()

    def get_timegrid_point(self, t, move="left"):
        """Return the next point to the left (or right) of the given `t` which
        is on the pulse time grid
        """
        t_start = self.tgrid[0]
        t_stop = self.tgrid[-1]
        dt = self.dt
        if t < t_start:
            return t_start
        if t > t_stop:
            return t_stop
        if move == "left":
            n = np.floor((t - t_start) / dt)
        else:
            n = np.ceil((t - t_start) / dt)
        return t_start + n * dt

    @property
    def fluence(self):
        """Fluence (integrated pulse energy) for the pulse

        .. math:: \\int_{-\\infty}^{\\infty} \\vert|E(t)\\vert^2 dt
        """
        return np.sum(self.amplitude ** 2) * float(self.dt)

    @property
    def oct_iter(self):
        """OCT iteration number from the pulse preamble, if available. If not
        available, 0"""
        iter_rx = re.compile(r'OCT iter[\s:]*(\d+)', re.I)
        for line in self.preamble:
            iter_match = iter_rx.search(line)
            if iter_match:
                return int(iter_match.group(1))
        return 0

    def spectrum(self, freq_unit=None, mode='complex', sort=False):
        """Calculate the spectrum of the pulse

        Parameters:

            freq_unit (str): Desired unit of the `freq` output array.
                Can Hz (GHz, Mhz, etc) to obtain frequencies, or any energy
                unit, using the correspondence ``f = E/h``. If not given,
                defaults to the `freq_unit` attribute
            mode (str): Wanted mode for `spectrum` output array.
                Possible values are 'complex', 'abs', 'real', 'imag'
            sort (bool): Sort the output `freq` array (and the output
                `spectrum` array) so that frequecies are ordered from
                ``-w_max .. 0 .. w_max``, instead of the direct output from the
                FFT. This is good for plotting, but does not allow to do an
                inverse Fourier transform afterwards

        Returns:

            numpy.ndarray(float), numpy.ndarray(complex): Frequency values
            associated with the amplitude values in `spectrum`, i.e. the x-axis
            of the spectrogram. The values are in the unit `freq_unit`.
            Real (`mode in ['abs', 'real', 'imag']`) or complex
            (`mode='complex'`) amplitude of each frequency component.

        Notes:

            If `sort=False` and `mode='complex'`, the original pulse
            values can be obtained by simply calling `np.fft.ifft`

            The spectrum is not normalized (Scipy follows the convention of
            doing the normalization on the backward transform). You might want
            to normalized by 1/n for plotting.
        """
        s = fft(self.amplitude)  # spectrum amplitude
        f = self.fftfreq(freq_unit=freq_unit)
        modifier = {
            'abs': lambda s: np.abs(s),
            'real': lambda s: np.real(s),
            'imag': lambda s: np.imag(s),
            'complex': lambda s: s,
        }
        if sort:
            order = np.argsort(f)
            f = f[order]
            s = s[order]
        return f, modifier[mode](s)

    def fftfreq(self, freq_unit=None):
        """Return the FFT frequencies associated with the pulse. Cf.
        `numpy.fft.fftfreq`

        Parameters:
            freq_unit (str): Desired unit of the output array.
                If not given, defaults to the `freq_unit` attribute

        Returns:
            numpy.ndarray(float): Frequency values associated with
            the pulse time grid.
            The first half of the `freq` array contains the
            positive frequencies, the second half the negative frequencies
        """
        if freq_unit is None:
            freq_unit = self.freq_unit
        n = len(self.amplitude)
        dt = float(self.unit_convert.convert(self.dt, self.time_unit, 'iu'))
        return self.unit_convert.convert(
            fftfreq(n, d=dt / (2.0 * np.pi)), 'iu', freq_unit
        )

    def derivative(self):
        """Calculate the derivative of the current pulse and return it as a new
        pulse. Note that the derivative is in units of `ampl_unit`/`time_unit`,
        but will be marked as 'unitless'.
        """
        self._unshift()
        T = self.tgrid[-1] - self.tgrid[0]
        deriv = scipy.fftpack.diff(self.amplitude) * (2.0 * np.pi / T)
        deriv_pulse = Pulse(
            tgrid=self.tgrid,
            amplitude=deriv,
            time_unit=self.time_unit,
            ampl_unit='unitless',
        )
        self._shift()
        deriv_pulse._shift()
        return deriv_pulse

    def phase(self, unwrap=False, s=None, derivative=False, freq_unit=None):
        """Return the pulse's complex phase, or derivative of the phase

        Parameters:
            unwrap (bool): If False, report the phase in ``[-pi:pi]``. If True,
                the phase may take any real value, avoiding the discontinuous
                jumps introduced by limiting the phase to a 2 pi interval.
            s (float or None): smoothing parameter, see
                :py:class:`scipy.interpolate.UnivariateSpline`. If None, no
                smoothing is performed.
            derivative (bool): If False, return the (smoothed) phase directly.
                If True, return the derivative of the (smoothed) phase.
            freq_unit (str or None): If `derivative` is True, the unit in which
                the derivative should be calculated. If None, `self.freq_unit`
                is used.

        Note:
            When calculating the derivative, some smoothing is generally
            required. By specifying a smoothing parameter `s`, the phase is
            smoothed through univeriate splines before calculating the
            derivative.

            When calculating the phase directly (instead of the derivative),
            smoothing should only be used when also unwrapping the phase.
        """

        phase = np.angle(self.amplitude)
        if unwrap or derivative:
            phase = np.unwrap(phase)

        tgrid = self.unit_convert.convert(self.tgrid, self.time_unit, 'iu')

        if derivative:

            if freq_unit is None:
                freq_unit = self.freq_unit
            if s is None:
                s = 1
            spl = UnivariateSpline(tgrid, phase, s=s)
            deriv = spl.derivative()(tgrid)
            return self.unit_convert.convert(deriv, 'iu', self.freq_unit)

        else:  # direct phase

            if s is not None:
                spl = UnivariateSpline(tgrid, phase, s=s)
                return spl(tgrid)
            else:
                return phase

    def write(self, filename, mode=None):
        """Write a pulse to file, in the same format as the QDYN `write_pulse`
        routine

        Parameters:

            filename (str): Name of file to which to write the pulse
            mode (str): Mode in which to write files. Possible values
                are 'abs', 'real', or 'complex'. The former two result in a
                two-column file, the latter in a three-column file. If not
                given, 'real' or 'complex' is used, depending on the type of
                :attr:`amplitude`
        """
        if mode is None:
            if iscomplexobj(self.amplitude):
                mode = 'complex'
            else:
                mode = 'real'
        self._check()
        preamble = self.preamble
        if not hasattr(preamble, '__getitem__'):
            preamble = [str(preamble)]
        postamble = self.postamble
        if not hasattr(postamble, '__getitem__'):
            postamble = [str(postamble)]
        buffer = ''
        # preamble
        for line in preamble:
            line = str(line).strip()
            if line.startswith('#'):
                buffer += "%s\n" % line
            else:
                buffer += '# %s\n' % line
        # header and data
        time_header = "time [%s]" % self.time_unit
        ampl_re_header = "Re(ampl) [%s]" % self.ampl_unit
        ampl_im_header = "Im(ampl) [%s]" % self.ampl_unit
        ampl_abs_header = "Abs(ampl) [%s]" % self.ampl_unit
        if mode == 'abs':
            buffer += "# %23s%25s\n" % (time_header, ampl_abs_header)
            for i, t in enumerate(self.tgrid):
                buffer += "%25.17E%25.17E\n" % (t, abs(self.amplitude[i]))
        elif mode == 'real':
            buffer += "# %23s%25s\n" % (time_header, ampl_re_header)
            for i, t in enumerate(self.tgrid):
                buffer += "%25.17E%25.17E\n" % (t, self.amplitude.real[i])
        elif mode == 'complex':
            buffer += "# %23s%25s%25s\n" % (
                time_header,
                ampl_re_header,
                ampl_im_header,
            )
            for i, t in enumerate(self.tgrid):
                buffer += "%25.17E%25.17E%25.17E\n" % (
                    t,
                    self.amplitude.real[i],
                    self.amplitude.imag[i],
                )
        else:
            raise ValueError("mode must be 'abs', 'real', or 'complex'")
        # postamble
        for line in self.postamble:
            line = str(line).strip()
            if line.startswith('#'):
                buffer += "%s\n" % line
            else:
                buffer += '# %s' % line

        with open_file(filename, 'w') as out_fh:
            out_fh.write(buffer)

    def write_oct_spectral_filter(self, filename, filter_func, freq_unit=None):
        """Evaluate a spectral filter function and write the result to the file
        with a given `filename`, in a format such that the file may be used for
        the `oct_spectral_filter` field of a pulse in a QDYN config file. The
        file will have two columns: The pulse frequencies (see `fftfreq`
        method), and the value of the filter function in the range [0, 1]

        Args:
            filename (str): Filename of the output file
            filter_func (callable): A function that takes a frequency values
                (in units of `freq_unit`) and returns a filter value in the
                range [0, 1]
            freq_unit (str):  Unit of frequencies that `filter_func`
                assumes.  If not given, defaults to the `freq_unit` attribute.

        Note:
            The `filter_func` function may return any values that numpy
            considers equivalent to floats in the range [0, 1]. This
            includes boolean values, where True is equivalent to 1.0 and
            False is equivalent to 0.0
        """
        if freq_unit is None:
            freq_unit = self.freq_unit
        freqs = self.fftfreq(freq_unit=freq_unit)
        filter = np.array([filter_func(f) for f in freqs], dtype=np.float64)
        if not (0 <= np.min(filter) <= 1 and 0 <= np.max(filter) <= 1):
            raise ValueError("filter values must be in the range [0, 1]")
        header = "%15s%15s" % ("freq [%s]" % freq_unit, 'filter')
        writetotxt(filename, freqs, filter, fmt='%15.7e%15.12f', header=header)

    def apply_spectral_filter(self, filter_func, freq_unit=None):
        """Apply a spectral filter function to the pulse (in place)

        Args:
            filter_func (callable): A function that takes a frequency values
                (in units of `freq_unit`) and returns a filter value in the
                range [0, 1]
            freq_unit (str):  Unit of frequencies that `filter_func`
                assumes.  If not given, defaults to the `freq_unit` attribute.
        """
        freqs, spec = self.spectrum(freq_unit=freq_unit)
        filter = np.array([filter_func(f) for f in freqs], dtype=np.float64)
        if not (0 <= np.min(filter) <= 1 and 0 <= np.max(filter) <= 1):
            raise ValueError("filter values must be in the range [0, 1]")
        spec *= filter
        self.amplitude = np.fft.ifft(spec)
        return self

    def apply_smoothing(self, **kwargs):
        """Smooth the pulse amplitude (in place) through univariate splining.
        All keyword arguments are passed directly to
        :py:class:`scipy.interpolate.UnivariateSpline`. This especially
        includes the smoothing parameter `s`.
        """
        if iscomplexobj(self.amplitude):
            splx = UnivariateSpline(self.tgrid, self.amplitude.real, **kwargs)
            sply = UnivariateSpline(self.tgrid, self.amplitude.imag, **kwargs)
            self.amplitude = splx(self.tgrid) + 1.0j * sply(self.tgrid)
        else:
            spl = UnivariateSpline(self.tgrid, self.amplitude, **kwargs)
            self.amplitude = spl(self.tgrid)
        return self

    def _unshift(self):
        """Move the pulse onto the unshifted time grid. This increases the
        number of points by one"""
        tgrid_new = np.linspace(
            float(self.t0), float(self.T), len(self.tgrid) + 1
        )
        pulse_new = np.zeros(
            len(self.amplitude) + 1, dtype=self.amplitude.dtype.type
        )
        pulse_new[0] = self.amplitude[0]
        for i in range(1, len(pulse_new) - 1):
            pulse_new[i] = 0.5 * (self.amplitude[i - 1] + self.amplitude[i])
        pulse_new[-1] = self.amplitude[-1]
        self.tgrid = tgrid_new
        self.amplitude = pulse_new
        self._check()

    def _shift(self, data=None):
        """Inverse of _unshift"""
        dt = float(self.dt)
        tgrid_new = np.linspace(
            self.tgrid[0] + dt / 2.0,
            self.tgrid[-1] - dt / 2.0,
            len(self.tgrid) - 1,
        )
        if data is None:
            data_old = self.amplitude
        else:
            data_old = data
        data_new = np.zeros(len(data_old) - 1, dtype=data_old.dtype.type)
        data_new[0] = data_old[0]
        for i in range(1, len(data_new) - 1):
            data_new[i] = 2.0 * data_old[i] - data_new[i - 1]
        data_new[-1] = data_old[-1]
        if data is None:
            self.tgrid = tgrid_new
            self.amplitude = data_new
            self._check()
        else:
            return data_new

    def resample(self, upsample=None, downsample=None, num=None, window=None):
        """Resample the pulse, either by giving an upsample ratio, a downsample
        ration, or a number of sampling points

        Parameters:

            upsample (int): Factor by which to increase the number of
                samples. Afterwards, those points extending beyond the original
                end point of the pulse are discarded.
            downsample (int): For ``downsample=n``, keep only every
                n'th point of the original pulse. This may cause the resampled
                pulse to end earlier than the original pulse
            num (int): Resample with `num` sampling points. This may
                case the end point of the resampled pulse to change
            window (list, numpy.ndarray, callable, str, float, or tuple):
                Specifies the window applied to the signal in the Fourier
                domain.  See `sympy.signal.resample`.

        Notes:

            Exactly one of `upsample`, `downsample`, or `num` must be given.

            Upsampling will maintain the pulse start and end point (as returned
            by the `T` and `t0` properties), up to some rounding errors.
            Downsampling, or using an arbitrary number will change the end
            point of the pulse in general.
        """
        self._unshift()
        nt = len(self.tgrid)

        if sum([(x is not None) for x in [upsample, downsample, num]]) != 1:
            raise ValueError(
                "Exactly one of upsample, downsample, or num must be given"
            )
        if num is None:
            if upsample is not None:
                upsample = int(upsample)
                num = nt * upsample
            elif downsample is not None:
                downsample = int(downsample)
                assert downsample > 0, "downsample must be > 0"
                num = nt / downsample
            else:
                num = nt
        else:
            num = num + 1  # to account for shifting

        a, t = signal.resample(self.amplitude, num, self.tgrid, window=window)

        if upsample is not None:
            # discard last (upsample-1) elements
            self.amplitude = a[: -(upsample - 1)]
            self.tgrid = t[: -(upsample - 1)]
        else:
            self.amplitude = a
            self.tgrid = t

        self._shift()

    def render_pulse(self, ax, label='pulse'):
        """Render the pulse amplitude on the given axes."""
        if np.max(np.abs(self.amplitude.imag)) > 0.0:
            ax.plot(self.tgrid, np.abs(self.amplitude), label=label)
            ax.set_ylabel("abs(pulse) (%s)" % self.ampl_unit)
        else:
            if np.min(self.amplitude.real) < 0:
                ax.axhline(y=0.0, ls='-', color='black')
            ax.plot(self.tgrid, self.amplitude.real, label=label)
            ax.set_ylabel("pulse (%s)" % (self.ampl_unit))
        ax.set_xlabel("time (%s)" % self.time_unit)

    def render_phase(self, ax, label='phase'):
        """Render the complex phase of the pulse on the given axes."""
        ax.axhline(y=0.0, ls='-', color='black')
        ax.plot(
            self.tgrid,
            np.angle(self.amplitude) / np.pi,
            ls='-',
            color='black',
            label=label,
        )
        ax.set_ylabel(r'phase ($\pi$)')
        ax.set_xlabel("time (%s)" % self.time_unit)

    def render_spectrum(
        self,
        ax,
        zoom=True,
        wmin=None,
        wmax=None,
        spec_scale=None,
        spec_max=None,
        freq_unit=None,
        mark_freqs=None,
        mark_freq_points=None,
        label='spectrum',
    ):
        """Render spectrum onto the given axis, see `plot` for arguments"""
        freq, spectrum = self.spectrum(
            mode='abs', sort=True, freq_unit=freq_unit
        )
        # normalizing the spectrum makes it independent of the number of
        # sampling points. That is, the spectrum of a signal that is simply
        # resampled will be the same as that of the original signal. Scipy
        # follows the convention of doing the normalization in the inverse
        # transform
        spectrum *= 1.0 / len(spectrum)

        if wmax is not None and wmin is not None:
            zoom = False
        if zoom:
            # figure out the range of the spectrum
            max_amp = np.amax(spectrum)
            if self.is_complex:
                # we center the spectrum around zero, and extend
                # symmetrically in both directions as far as there is
                # significant amplitude
                wmin = np.max(freq)
                wmax = np.min(freq)
                for i, w in enumerate(freq):
                    if spectrum[i] > 0.001 * max_amp:
                        if w > wmax:
                            wmax = w
                        if w < wmin:
                            wmin = w
                wmax = max(abs(wmin), abs(wmax))
                wmin = -wmax
            else:
                # we show only the positive part of the spectrum (under the
                # assumption that the spectrum is symmetric) and zoom in
                # only on the region that was significant amplitude
                wmin = 0.0
                wmax = 0.0
                for i, w in enumerate(freq):
                    if spectrum[i] > 0.001 * max_amp:
                        if wmin == 0 and w > 0:
                            wmin = w
                        wmax = w
            buffer = (wmax - wmin) * 0.1

        # plot spectrum
        if zoom:
            ax.set_xlim((wmin - buffer), (wmax + buffer))
        else:
            if wmin is not None and wmax is not None:
                ax.set_xlim(wmin, wmax)
        ax.set_xlabel("frequency (%s)" % freq_unit)
        ax.set_ylabel("abs(spec) (arb. un.)")
        if spec_scale is None:
            spec_scale = 1.0
        ax.plot(
            freq, spec_scale * spectrum, marker=mark_freq_points, label=label
        )
        if spec_max is not None:
            ax.set_ylim(0, spec_max)
        if mark_freqs is not None:
            for freq in mark_freqs:
                kwargs = {'ls': '--', 'color': 'black'}
                try:
                    freq, kwargs = freq
                except TypeError:
                    pass
                ax.axvline(x=float(freq), **kwargs)

    def plot(
        self,
        fig=None,
        show_pulse=True,
        show_spectrum=True,
        zoom=True,
        wmin=None,
        wmax=None,
        spec_scale=None,
        spec_max=None,
        freq_unit=None,
        mark_freqs=None,
        mark_freq_points=None,
        **figargs
    ):
        """Generate a plot of the pulse on a given figure

        Parameters:

            fig (matplotlib.figure.Figure): The figure onto which to plot. If
                not given, create a new figure from `matplotlib.pyplot.figure`
            show_pulse (bool): Include a plot of the pulse amplitude? If the
                pulse has a vanishing imaginary part, the plot will show the
                real part of the amplitude, otherwise, there will be one plot
                for the absolute value of the amplitude and one showing the
                complex phase in units of pi
            show_spectrum (bool): Include a plot of the spectrum?
            zoom (bool): If `True`, only show the part of the spectrum that has
                amplitude of at least 0.1% of the maximum peak in the spectrum.
                For real pulses, only the positive part of the spectrum is
                shown
            wmin (float): Lowest frequency to show. Overrides zoom options.
                Must be given together with `wmax`.
            wmax (float): Highest frequency to show. Overrides zoom options.
                Must be given together with `wmin`.
            spec_scale (float): Factor by which to scale the amplitudes in the
                spectrum
            spec_max (float): Maximum amplitude in the spectrum, after
                spec_scale has been applied
            freq_unit (str): Unit in which to show the frequency axis in the
                spectrum. If not given, use the `freq_unit` attribute
            mark_freqs (None, list(float), list((float, dict))):
                Array of frequencies to mark in spectrum as vertical dashed
                lines.  If list of tuples (float, dict), the float value is the
                frequency to mark, and the dict gives the keyword arguments
                that are passed to the matplotlib `axvline` method.
            mark_freq_points (None, ~matplotlib.markers.MarkerStyle): Marker to
                be used to indicate the individual points in the spectrum.

        The remaining figargs are passed to `matplotlib.pyplot.figure` to
        create a new figure if `fig` is None.
        """
        if fig is None:
            fig = plt.figure(**figargs)

        if freq_unit is None:
            freq_unit = self.freq_unit

        self._check()
        pulse_is_complex = self.is_complex

        # do the layout
        if show_pulse and show_spectrum:
            if pulse_is_complex:
                # show abs(pulse), phase(pulse), abs(spectrum)
                gs = GridSpec(3, 1, height_ratios=[2, 1, 2])
            else:
                # show real(pulse), abs(spectrum)
                gs = GridSpec(2, 1, height_ratios=[1, 1])
        else:
            if show_pulse:
                if pulse_is_complex:
                    # show abs(pulse), phase(pulse)
                    gs = GridSpec(2, 1, height_ratios=[2, 1])
                else:
                    # show real(pulse)
                    gs = GridSpec(1, 1)
            else:
                gs = GridSpec(1, 1)

        if show_spectrum:
            ax_spectrum = fig.add_subplot(gs[-1], label='spectrum')
            self.render_spectrum(
                ax_spectrum,
                zoom,
                wmin,
                wmax,
                spec_scale,
                spec_max,
                freq_unit,
                mark_freqs,
                mark_freq_points,
            )
        if show_pulse:
            # plot pulse amplitude
            ax_pulse = fig.add_subplot(gs[0], label='pulse')
            self.render_pulse(ax_pulse)
            if pulse_is_complex:
                # plot pulse phase
                ax_phase = fig.add_subplot(gs[1], label='phase')
                self.render_phase(ax_phase)

        fig.subplots_adjust(hspace=0.3)

    def show(self, **kwargs):
        """Show a plot of the pulse and its spectrum. All arguments will be
        passed to the plot method
        """
        self.plot(**kwargs)  # uses plt.figure()
        plt.show()

    def show_pulse(self, **kwargs):
        """Show a plot of the pulse amplitude; alias for
        `show(show_spectrum=False)`. All other arguments will be passed to the
        `show` method
        """
        self.show(show_spectrum=False, **kwargs)

    def show_spectrum(self, zoom=True, freq_unit=None, **kwargs):
        """Show a plot of the pulse spectrum; alias for
        `show(show_pulse=False, zoom=zoom, freq_unit=freq_unit)`. All other
        arguments will be passed to the `show` method
        """
        self.show(show_pulse=False, zoom=zoom, freq_unit=freq_unit, **kwargs)


def pulse_tgrid(T, nt, t0=0.0):
    """Return a pulse time grid suitable for an equidistant time grid of the
    states between t0 and T with nt intervals. The values of the pulse are
    defined in the intervals of the time grid, so the pulse time grid will be
    shifted by dt/2 with respect to the time grid of the states. Also, the
    pulse time grid will have nt-1 points:

    >>> print(", ".join([("%.2f" % t) for t in pulse_tgrid(1.5, nt=4)]))
    0.25, 0.75, 1.25

    The limits of the states time grid are defined as the starting and end
    points of the pulse, however:

    >>> p = Pulse(tgrid=pulse_tgrid(1.5, 4), time_unit='ns', ampl_unit='MHz')
    >>> p.t0
    0
    >>> p.T
    1.5_ns
    """
    dt = float(T - t0) / (nt - 1)
    t_first_pulse = float(t0) + 0.5 * dt
    t_last_pulse = float(T) - 0.5 * dt
    nt_pulse = nt - 1
    return np.linspace(t_first_pulse, t_last_pulse, nt_pulse)


def tgrid_from_config(tgrid_dict, time_unit, pulse_grid=True):
    """Extract the time grid from the given config file

    >>> tgrid_dict = dict([('t_start', 0.0), ('t_stop', UnitFloat(10.0, 'ns')),
    ...                    ('dt', UnitFloat(20, 'ps')), ('fixed', True)])
    >>> tgrid = tgrid_from_config(tgrid_dict, time_unit='ns')
    >>> print("%.2f" % tgrid[0])
    0.01
    >>> print("%.2f" % tgrid[-1])
    9.99
    """
    if time_unit is None:
        time_unit = 'unitless'
    t_start = None
    t_stop = None
    nt = None
    dt = None
    if 't_start' in tgrid_dict:
        t_start = tgrid_dict['t_start']
    if 't_stop' in tgrid_dict:
        t_stop = tgrid_dict['t_stop']
    if 'nt' in tgrid_dict:
        nt = tgrid_dict['nt']
    if 'dt' in tgrid_dict:
        dt = tgrid_dict['dt']
    if t_start is None:
        assert (
            (t_stop is not None) and (dt is not None) and (nt is not None)
        ), "tgrid not fully specified in config"
        t_start = t_stop - (nt - 1) * dt
    if t_stop is None:
        assert (
            (t_start is not None) and (dt is not None) and (nt is not None)
        ), "tgrid not fully specified in config"
        t_stop = t_start + (nt - 1) * dt
    if nt is None:
        assert (
            (t_start is not None) and (dt is not None) and (t_stop is not None)
        ), "tgrid not fully specified in config"
        nt = int((t_stop - t_start) / dt) + 1
    if dt is None:
        assert (
            (t_start is not None) and (nt is not None) and (t_stop is not None)
        ), "tgrid not fully specified in config"
        dt = (t_stop - t_start) / float(nt - 1)
    t_start = UnitFloat(t_start).convert(time_unit)
    t_stop = UnitFloat(t_stop).convert(time_unit)
    dt = UnitFloat(dt).convert(time_unit)
    if pulse_grid:
        # convert to pulse parameters
        t_start += 0.5 * dt
        t_stop -= 0.5 * dt
        nt -= 1
    tgrid = np.linspace(float(t_start), float(t_stop), nt)
    return tgrid


###############################################################################
# Shape functions
###############################################################################


def carrier(
    t, time_unit, freq, freq_unit, weights=None, phases=None, complex=False
):
    r'''Create the "carrier" of the pulse as a weighted superposition of
    cosines at different frequencies.

    Parameters:

        t (numpy.ndarray(float)): Time value or time grid
        time_unit (str): Unit of `t`
        freq (numpy.ndarray(float)): Carrier frequency or frequencies
        freq_unit (str): Unit of `freq`
        weights (numpy.ndarray): If `freq` is an array, weights for
            the different frequencies. If not given, all weights are 1. The
            weights are normalized to sum to one.  Any weight smaller than
            machine precision is assumed zero.
        phases (numpy.ndarray): If `phases` is an array, phase shift
            for each frequency component, in units of pi. If not given, all
            phases are 0.
        complex (bool): If `True`, oscillate in the complex plane

    Returns:

        numpy.ndarray(complex): Depending on whether
        `complex` is `True` or `False`,

        .. math::
            s(t) = \sum_j  w_j * \cos(\omega_j * t + \phi_j) \\
            s(t) = \sum_j  w_j * \exp(i*(\omega_j * t + \phi_j))

        with :math:`\omega_j = 2 * \pi * f_j`, and frequency
        :math:`f_j` where :math:`f_j` is the j'th value in `freq`. The
        value of :math:`\phi_j` is the j'th value in `phases`

        `signal` is a scalar if `t` is a scalar, and and array if `t`
        is an array

    Notes:

        `freq_unit` can be Hz (GHz, MHz, etc), describing the frequency
        directly, or any energy unit, in which case the energy value E (given
        through the freq parameter) is converted to an actual frequency as

        .. math::
            f = E / (\hbar * 2 * \pi)
    '''
    unit_convert = UnitConvert()
    if np.isscalar(t):
        signal = 0.0
    else:
        signal = np.zeros(len(t), dtype=np.complex128)
        assert isinstance(t, np.ndarray), "t must be numpy array"
        assert t.dtype.type is np.float64, "t must be double precision real"
    c = unit_convert.convert(1, time_unit, 'iu') * unit_convert.convert(
        1, freq_unit, 'iu'
    )
    if np.isscalar(freq):
        if complex:
            signal += np.exp(1j * c * freq * t)  # element-wise
        else:
            signal += np.cos(c * freq * t)  # element-wise
    else:
        eps = 1.0e-16  # machine precision
        if weights is None:
            weights = np.ones(len(freq))
        if phases is None:
            phases = np.zeros(len(freq))
        norm = float(sum(weights))
        if norm > eps:
            for (w, weight, phase) in zip(freq, weights, phases):
                if weight > eps:
                    weight = weight / norm
                    if complex:
                        signal += weight * np.exp(
                            1j * (c * w * t + phase * np.pi)
                        )
                    else:
                        signal += weight * np.cos(c * w * t + phase * np.pi)
    return signal


def CRAB_carrier(
    t, time_unit, freq, freq_unit, a, b, normalize=False, complex=False
):
    r'''Construct a "carrier" based on the CRAB formula

        .. math::
            E(t) = \sum_{n} (a_n \cos(\omega_n t) + b_n \cos(\omega_n t))

    where :math:`a_n` is the n'th element of `a`, :math:`b_n` is the n'th
    element of `b`, and :math:`\omega_n` is the n'th element of freq.

    Args:
        t (numpy.ndarray): time grid values
        time_unit (str): Unit of `t`
        freq (numpy.ndarray): Carrier frequency or frequencies
        freq_unit (str): Unit of `freq`
        a (numpy.ndarray): Coefficients for cosines
        b (numpy.ndarray): Coefficients for sines
        normalize (bool): If True, normalize the resulting carrier
            such that its values are in [-1,1]
        complex (bool): If True, oscillate in the complex
            plane

            .. math::
                E(t) = \sum_{n} (a_n - i b_n) \exp(i \omega_n t)

    Notes:
        `freq_unit` can be Hz (GHz, MHz, etc), describing the frequency
        directly, or any energy unit, in which case the energy value E (given
        through the freq parameter) is converted to an actual frequency as

        .. math::
            f = E / (\hbar * 2 * \pi)
    '''
    unit_convert = UnitConvert()
    c = unit_convert.convert(1, time_unit, 'iu') * unit_convert.convert(
        1, freq_unit, 'iu'
    )
    assert (
        len(a) == len(b) == len(freq)
    ), "freq, a, b must all be of the same length"
    if complex:
        signal = np.zeros(len(t), dtype=np.complex128)
    else:
        signal = np.zeros(len(t), dtype=np.float64)
    for w_n, a_n, b_n in zip(freq, a, b):
        if complex:
            signal += (a_n - 1j * b_n) * np.exp(1j * c * w_n * t)
        else:
            signal += a_n * np.cos(c * w_n * t) + b_n * np.sin(c * w_n * t)
    if normalize:
        nrm = np.abs(signal).max()
        if nrm > 1.0e-16:
            signal *= 1.0 / nrm
    return signal


def gaussian(t, t0, sigma):
    """Return a Gaussian shape with peak amplitude 1.0

    Parameters:

        t (float, numpy.ndarray): time value or grid
        t0 (float): center of peak
        sigma (float): width of Gaussian

    Returns:

        (float, numpy.ndarray): Gaussian shape of same type as `t`

    """
    return np.exp(-(t - t0) ** 2 / (2 * sigma ** 2))


@np.vectorize
def box(t, t_start, t_stop):
    """Return a box-shape (Theta-function) that is zero before `t_start` and
    after `t_stop` and one elsewehere.

    Parameters:

        t (scalar, numpy.ndarray): Time point or time grid
        t_start (scalar): First value of `t` for which the box has value 1
        t_stop (scalar): Last value of `t` for which the box has value 1

    Returns:

        box_shape (numpy.ndarray(float)): If `t` is an array, `box_shape` is
            an array of the same size as `t` If `t` is scalar, `box_shape` is
            an array of size 1 (which for all intents and purposes can be used
            like a float)
    """
    if t < t_start:
        return 0.0
    if t > t_stop:
        return 0.0
    return 1.0


def blackman(t, t_start, t_stop, a=0.16):
    """Return a Blackman function between `t_start` and `t_stop`,
    see http://en.wikipedia.org/wiki/Window_function#Blackman_windows

    A Blackman shape looks nearly identical to a Gaussian with a 6-sigma
    interval between start and stop  Unlike the Gaussian,
    however, it will go exactly to zero at the edges. Thus, Blackman pulses
    are often preferable to Gaussians.

    Parameters:

        t (float, numpy.ndarray): Time point or time grid
        t_start (float): Starting point of Blackman shape
        t_stop (float): End point of Blackman shape

    Returns:

        (float, numpy.ndarray(float)):
            If `t` is a scalar, `blackman_shape` is the scalar value of the
            Blackman shape at `t`.  If `t` is an array, `blackman_shape` is an
            array of same size as `t`, containing the values for the Blackman
            shape (zero before `t_start` and after `t_stop`)

    See Also:

        numpy.blackman
    """
    T = t_stop - t_start
    return (
        0.5
        * (
            1.0
            - a
            - np.cos(2.0 * np.pi * (t - t_start) / T)
            + a * np.cos(4.0 * np.pi * (t - t_start) / T)
        )
        * box(t, t_start, t_stop)
    )


@np.vectorize
def flattop(t, t_start, t_stop, t_rise, t_fall=None):
    """Return flattop shape, starting at `t_start` with a sine-squared ramp
    that goes to 1 within `t_rise`, and ramps down to 0 again within `t_fall`
    from `t_stop`

    Parameters:

        t (scalar, numpy.ndarray): Time  point or time grid
        t_start (scalar): Start of flattop window
        t_stop (scalar): Stop of flattop window
        t_rise (scalar): Duration of ramp-up, starting at `t_start`
        t_fall (scalar): Duration of ramp-down, ending at `t_stop`.
            If not given, `t_fall=t_rise`.

    Returns:

        flattop_shape (numpy.ndarray(float)): If `t` is an array,
            `flattop_shape` is an array of the same size as `t` If `t` is
            scalar, `flattop_ox_shape` is an array of size 1 (which for all
            intents and purposes can be used like a float)
    """
    if t_start <= t <= t_stop:
        f = 1.0
        if t_fall is None:
            t_fall = t_rise
        if t <= t_start + t_rise:
            f = np.sin(np.pi * (t - t_start) / (2.0 * t_rise)) ** 2
        elif t >= t_stop - t_fall:
            f = np.sin(np.pi * (t - t_stop) / (2.0 * t_fall)) ** 2
        return f
    else:
        return 0.0
