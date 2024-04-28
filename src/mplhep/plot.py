from __future__ import annotations

import collections.abc
import inspect
import logging
from collections import OrderedDict, namedtuple
from typing import TYPE_CHECKING, Any, Union

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.offsetbox import AnchoredText
from matplotlib.transforms import Bbox
from mpl_toolkits.axes_grid1 import axes_size, make_axes_locatable

from .utils import (
    Plottable,
    get_histogram_axes_title,
    get_plottable_protocol_bins,
    hist_object_handler,
    isLight,
    process_histogram_parts,
    align_marker,
    to_padded2d,
)

if TYPE_CHECKING:
    from numpy.typing import ArrayLike

StairsArtists = namedtuple("StairsArtists", "stairs errorbar legend_artist")
ErrorBarArtists = namedtuple("ErrorBarArtists", "errorbar")
ColormeshArtists = namedtuple("ColormeshArtists", "pcolormesh cbar text")

Hist1DArtists = Union[StairsArtists, ErrorBarArtists]
Hist2DArtists = ColormeshArtists


def soft_update_kwargs(kwargs, mods, rc=True):
    not_default = [k for k, v in mpl.rcParamsDefault.items() if v != mpl.rcParams[k]]
    respect = [
        "hatch.linewidth",
        "lines.linewidth",
        "patch.linewidth",
        "lines.linestyle",
    ]
    aliases = {"ls": "linestyle", "lw": "linewidth"}
    kwargs = {aliases[k] if k in aliases else k: v for k, v in kwargs.items()}
    for key, val in mods.items():
        rc_modded = (key in not_default) or (
            key in [k.split(".")[-1] for k in not_default if k in respect]
        )
        if key not in kwargs and (rc and not rc_modded):
            kwargs[key] = val
    return kwargs


########################################
# Histogram plotter
def histplot(
    H,  # Histogram object, tuple or array
    bins=None,  # Bins to be supplied when h is a value array or iterable of array
    *,
    yerr: ArrayLike | bool | None = None,
    w2=None,
    w2method=None,
    stack: bool = False,
    density: bool = False,
    binwnorm=None,
    histtype: str = "step",
    xerr=False,
    label=None,
    sort=None,
    edges=True,
    binticks=False,
    ax: mpl.axes.Axes | None = None,
    flow="hint",
    **kwargs,
):
    """
    Create a 1D histogram plot from `np.histogram`-like inputs.

    Parameters
    ----------
        H : object
            Histogram object with containing values and optionally bins. Can be:

            - `np.histogram` tuple
            - PlottableProtocol histogram object
            - `boost_histogram` classic (<0.13) histogram object
            - raw histogram values, provided `bins` is specified.

            Or list thereof.
        bins : iterable, optional
            Histogram bins, if not part of ``H``.
        yerr : iterable or bool, optional
            Histogram uncertainties. Following modes are supported:
            - True, sqrt(N) errors or poissonian interval when ``w2`` is specified
            - shape(N) array of for one sided errors or list thereof
            - shape(Nx2) array of for two sided errors or list thereof
        w2 : iterable, optional
            Sum of the histogram weights squared for poissonian interval error
            calculation
        w2method: callable, optional
            Function calculating CLs with signature ``low, high = fcn(w, w2)``. Here
            ``low`` and ``high`` are given in absolute terms, not relative to w.
            Default is ``None``. If w2 has integer values (likely to be data) poisson
            interval is calculated, otherwise the resulting error is symmetric
            ``sqrt(w2)``. Specifying ``poisson`` or ``sqrt`` will force that behaviours.
        stack : bool, optional
            Whether to stack or overlay non-axis dimension (if it exists). N.B. in
            contrast to ROOT, stacking is performed in a single call aka
            ``histplot([h1, h2, ...], stack=True)`` as opposed to multiple calls.
        density : bool, optional
            If true, convert sum weights to probability density (i.e. integrates to 1
            over domain of axis) (Note: this option conflicts with ``binwnorm``)
        binwnorm : float, optional
            If true, convert sum weights to bin-width-normalized, with unit equal to
                supplied value (usually you want to specify 1.)
        histtype: {'step', 'fill', 'band', 'errorbar'}, optional, default: "step"
            Type of histogram to plot:

            - "step": skyline/step/outline of a histogram using `plt.stairs <https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.stairs.html#matplotlib-axes-axes-stairs>`_
            - "fill": filled histogram using `plt.stairs <https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.stairs.html#matplotlib-axes-axes-stairs>`_
            - "step": filled band spanning the yerr range of the histogram using `plt.stairs <https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.stairs.html#matplotlib-axes-axes-stairs>`_
            - "errorbar": single marker histogram using `plt.errorbar <https://matplotlib.org/stable/api/_as_gen/matplotlib.axes.Axes.errorbar.html#matplotlib-axes-axes-errorbar>`_
        xerr:  bool or float, optional
            Size of xerr if ``histtype == 'errorbar'``. If ``True``, bin-width will be used.
        label : str or list, optional
            Label for legend entry.
        sort: {'label'/'l', 'yield'/'y'}, optional
            Append '_r' for reverse.
        edges : bool, default: True, optional
            Specifies whether to draw first and last edges of the histogram
        binticks : bool, default: False, optional
            Attempts to draw x-axis ticks coinciding with bin boundaries if feasible.
        ax : matplotlib.axes.Axes, optional
            Axes object (if None, last one is fetched or one is created)
        flow :  str, optional { "show", "sum", "hint", "none"}
            Whether plot the under/overflow bin. If "show", add additional under/overflow bin.
            If "sum", add the under/overflow bin content to first/last bin.
        **kwargs :
            Keyword arguments passed to underlying matplotlib functions -
            {'stairs', 'errorbar'}.
    Returns
    -------
        List[Hist1DArtists]

    """

    # ax check
    if ax is None:
        ax = plt.gca()
    else:
        if not isinstance(ax, plt.Axes):
            raise ValueError("ax must be a matplotlib Axes object")

    # arg check
    _allowed_histtype = ["fill", "step", "errorbar", "band"]
    _err_message = f"Select 'histtype' from: {_allowed_histtype}"
    assert histtype in _allowed_histtype, _err_message
    assert flow is None or flow in {
        "show",
        "sum",
        "hint",
        "none",
    }, "flow must be show, sum, hint, or none"

    # Convert 1/0 etc to real bools
    stack = bool(stack)
    density = bool(density)
    edges = bool(edges)
    binticks = bool(binticks)

    # Process input
    hists = list(process_histogram_parts(H, bins))
    final_bins, xtick_labels = get_plottable_protocol_bins(hists[0].axes[0])
    _bin_widths = np.diff(final_bins)
    _bin_centers = final_bins[1:] - _bin_widths / float(2)
    assert final_bins.ndim == 1, "bins need to be 1 dimensional"
    _x_axes_label = ax.get_xlabel()
    x_axes_label = (
        _x_axes_label
        if _x_axes_label != ""
        else get_histogram_axes_title(hists[0].axes[0])
    )

    plottables = []
    flow_bins = final_bins
    for i, h in enumerate(hists):
        value, variance = np.copy(h.values()), h.variances()
        if has_variances := variance is not None:
            variance = np.copy(variance)
        underflow, overflow = 0.0, 0.0
        underflowv, overflowv = 0.0, 0.0
        # One sided flow bins - hist (uproot hist does not have the over- or underflow traits)
        if (
            hasattr(h, "axes")
            and (traits := getattr(h.axes[0], "traits", None)) is not None
            and hasattr(traits, "underflow")
            and hasattr(traits, "overflow")
        ):
            if traits.overflow:
                overflow = np.copy(h.values(flow=True))[-1]
                if has_variances:
                    overflowv = np.copy(h.variances(flow=True))[-1]
            if traits.underflow:
                underflow = np.copy(h.values(flow=True))[0]
                if has_variances:
                    underflowv = np.copy(h.variances(flow=True))[0]
        # Both flow bins exist - uproot
        elif hasattr(h, "values") and "flow" in inspect.getfullargspec(h.values).args:
            if len(h.values()) + 2 == len(
                h.values(flow=True)
            ):  # easy case, both over/under
                underflow, overflow = (
                    np.copy(h.values(flow=True))[0],
                    np.copy(h.values(flow=True))[-1],
                )
                if has_variances:
                    underflowv, overflowv = (
                        np.copy(h.variances(flow=True))[0],
                        np.copy(h.variances(flow=True))[-1],
                    )

        # Set plottables
        if flow == "none":
            plottables.append(Plottable(value, edges=final_bins, variances=variance))
        elif flow == "hint":  # modify plottable
            plottables.append(Plottable(value, edges=final_bins, variances=variance))
        elif flow == "show":
            _flow_bin_size = np.max(
                [0.05 * (final_bins[-1] - final_bins[0]), np.mean(np.diff(final_bins))]
            )
            flow_bins = np.copy(final_bins)
            if underflow > 0:
                flow_bins = np.r_[flow_bins[0] - _flow_bin_size, flow_bins]
                value = np.r_[underflow, value]
                if has_variances:
                    variance = np.r_[underflowv, variance]
            if overflow > 0:
                flow_bins = np.r_[flow_bins, flow_bins[-1] + _flow_bin_size]
                value = np.r_[value, overflow]
                if has_variances:
                    variance = np.r_[variance, overflowv]
            plottables.append(Plottable(value, edges=flow_bins, variances=variance))
        elif flow == "sum":
            if underflow > 0:
                value[0] += underflow
                if has_variances:
                    variance[0] += underflowv
            if overflow > 0:
                value[-1] += overflow
                if has_variances:
                    variance[-1] += overflowv
            plottables.append(Plottable(value, edges=final_bins, variances=variance))
        else:
            plottables.append(Plottable(value, edges=final_bins, variances=variance))

    if w2 is not None:
        for _w2, _plottable in zip(
            w2.reshape(len(plottables), len(final_bins) - 1), plottables
        ):
            _plottable.variances = _w2
            _plottable.method = w2method

    if w2 is not None and yerr is not None:
        raise ValueError("Can only supply errors or w2")

    _labels: list[str | None]
    if label is None:
        _labels = [None] * len(plottables)
    elif isinstance(label, str):
        _labels = [label] * len(plottables)
    elif not np.iterable(label):
        _labels = [str(label)] * len(plottables)
    else:
        _labels = [str(lab) for lab in label]

    def iterable_not_string(arg):
        return isinstance(arg, collections.abc.Iterable) and not isinstance(arg, str)

    _chunked_kwargs: list[dict[str, Any]] = []
    for _ in range(len(plottables)):
        _chunked_kwargs.append({})
    for kwarg in kwargs:
        # Check if iterable
        if iterable_not_string(kwargs[kwarg]):
            # Check if tuple of floats or ints (can be used for colors)
            if isinstance(kwargs[kwarg], tuple) and all(
                isinstance(x, int) or isinstance(x, float) for x in kwargs[kwarg]
            ):
                for i in range(len(_chunked_kwargs)):
                    _chunked_kwargs[i][kwarg] = kwargs[kwarg]
            else:
                for i, kw in enumerate(kwargs[kwarg]):
                    _chunked_kwargs[i][kwarg] = kw
        else:
            for i in range(len(_chunked_kwargs)):
                _chunked_kwargs[i][kwarg] = kwargs[kwarg]

    ############################
    # # yerr calculation
    _yerr: np.ndarray | None
    if yerr is not None:
        # yerr is array
        if hasattr(yerr, "__len__"):
            _yerr = np.asarray(yerr)
        # yerr is a number
        elif isinstance(yerr, (int, float)) and not isinstance(yerr, bool):
            _yerr = np.ones((len(plottables), len(final_bins) - 1)) * yerr
        # yerr is automatic
        else:
            _yerr = None
    else:
        _yerr = None

    if _yerr is not None:
        assert isinstance(_yerr, np.ndarray)
        if _yerr.ndim == 3:
            # Already correct format
            pass
        elif _yerr.ndim == 2 and len(plottables) == 1:
            # Broadcast ndim 2 to ndim 3
            if _yerr.shape[-2] == 2:  # [[1,1], [1,1]]
                _yerr = _yerr.reshape(len(plottables), 2, _yerr.shape[-1])
            elif _yerr.shape[-2] == 1:  # [[1,1]]
                _yerr = np.tile(_yerr, 2).reshape(len(plottables), 2, _yerr.shape[-1])
            else:
                raise ValueError("yerr format is not understood")
        elif _yerr.ndim == 2:
            # Broadcast yerr (nh, N) to (nh, 2, N)
            _yerr = np.tile(_yerr, 2).reshape(len(plottables), 2, _yerr.shape[-1])
        elif _yerr.ndim == 1:
            # Broadcast yerr (1, N) to (nh, 2, N)
            _yerr = np.tile(_yerr, 2 * len(plottables)).reshape(
                len(plottables), 2, _yerr.shape[-1]
            )
        else:
            raise ValueError("yerr format is not understood")

        assert _yerr is not None
        for yrs, _plottable in zip(_yerr, plottables):
            _plottable.fixed_errors(*yrs)

    # Sorting
    if sort is not None:
        if isinstance(sort, str):
            if sort.split("_")[0] in ["l", "label"] and isinstance(_labels, list):
                order = np.argsort(label)  # [::-1]
            elif sort.split("_")[0] in ["y", "yield"]:
                _yields = [np.sum(_h.values) for _h in plottables]
                order = np.argsort(_yields)
            if len(sort.split("_")) == 2 and sort.split("_")[1] == "r":
                order = order[::-1]
        elif isinstance(sort, list) or isinstance(sort, np.ndarray):
            if len(sort) != len(plottables):
                raise ValueError(
                    f"Sort indexing array is of the wrong size - {len(sort)}, {len(plottables)} expected."
                )
            order = np.asarray(sort)
        else:
            raise ValueError(f"Sort type: {sort} not understood.")
        plottables = [plottables[ix] for ix in order]
        _chunked_kwargs = [_chunked_kwargs[ix] for ix in order]
        _labels = [_labels[ix] for ix in order]

    # ############################
    # # Stacking, norming, density
    if density is True and binwnorm is not None:
        raise ValueError("Can only set density or binwnorm.")
    if density is True:
        if stack:
            _total = np.sum(
                np.array([plottable.values for plottable in plottables]), axis=0
            )
            for plottable in plottables:
                plottable.flat_scale(1.0 / np.sum(np.diff(final_bins) * _total))
        else:
            for plottable in plottables:
                plottable.density = True
    elif binwnorm is not None:
        for plottable, norm in zip(
            plottables, np.broadcast_to(binwnorm, (len(plottables),))
        ):
            plottable.flat_scale(norm)
            plottable.binwnorm()

    # Stack
    if stack and len(plottables) > 1:
        from .utils import stack as stack_fun

        plottables = stack_fun(*plottables)

    ##########
    # Plotting
    return_artists: list[StairsArtists | ErrorBarArtists] = []
    if histtype == "step":
        for i in range(len(plottables)):
            do_errors = yerr is not False and (
                (yerr is not None or w2 is not None)
                or (plottables[i].variances is not None)
            )

            _kwargs = _chunked_kwargs[i]
            _label = _labels[i] if do_errors else None
            _step_label = _labels[i] if not do_errors else None
            _kwargs = soft_update_kwargs(_kwargs, {"linewidth": 1.5})

            _plot_info = plottables[i].to_stairs()
            _plot_info["baseline"] = None if not edges else 0
            _s = ax.stairs(
                **_plot_info,
                label=_step_label,
                **_kwargs,
            )

            if do_errors:
                _kwargs = soft_update_kwargs(_kwargs, {"color": _s.get_edgecolor()})
                _ls = _kwargs.pop("linestyle", "-")
                _kwargs["linestyle"] = "none"
                _plot_info = plottables[i].to_errorbar()
                _e = ax.errorbar(
                    **_plot_info,
                    **_kwargs,
                )
                _e_leg = ax.errorbar(
                    [], [], yerr=1, xerr=None, color=_s.get_edgecolor(), label=_label, linestyle=_ls
                )
            return_artists.append(
                StairsArtists(
                    _s,
                    _e if do_errors else None,
                    _e_leg if do_errors else None,
                )
            )
        _artist = _s

    elif histtype == "fill":
        for i in range(len(plottables)):
            _kwargs = _chunked_kwargs[i]
            _f = ax.stairs(
                **plottables[i].to_stairs(), label=_labels[i], fill=True, **_kwargs
            )
            return_artists.append(StairsArtists(_f, None, None))
        _artist = _f

    elif histtype == "band":
        band_defaults = {
            "alpha": 0.5,
            "edgecolor": "darkgray",
            "facecolor": "whitesmoke",
            "hatch": "////  /",
        }
        for i in range(len(plottables)):
            _kwargs = _chunked_kwargs[i]
            _f = ax.stairs(
                **plottables[i].to_stairband(),
                label=_labels[i],
                fill=True,
                **soft_update_kwargs(_kwargs, band_defaults),
            )
            return_artists.append(StairsArtists(_f, None, None))
        _artist = _f

    elif histtype == "errorbar":
        err_defaults = {
            "linestyle": "none",
            "marker": ".",
            "markersize": 10.0,
            "elinewidth": 1,
        }

        _xerr: np.ndarray | float | int | None

        if xerr is True:
            _xerr = _bin_widths / 2
        elif isinstance(xerr, (int, float)) and not isinstance(xerr, bool):
            _xerr = xerr
        else:
            _xerr = None

        for i in range(len(plottables)):
            _kwargs = _chunked_kwargs[i]
            _plot_info = plottables[i].to_errorbar()
            if yerr is False:
                _plot_info["yerr"] = None
            _plot_info["xerr"] = _xerr
            _e = ax.errorbar(
                **_plot_info,
                label=_labels[i],
                **soft_update_kwargs(_kwargs, err_defaults),
            )
            return_artists.append(ErrorBarArtists(_e))

        _artist = _e[0]

    # Add sticky edges for autoscale
    assert hasattr(
        listy := _artist.sticky_edges.y, "append"
    ), "cannot append to sticky edges"
    listy.append(0)

    if xtick_labels is None or flow == "show":
        if binticks:
            _slice = int(round(float(len(final_bins)) / len(ax.get_xticks()))) + 1
            ax.set_xticks(final_bins[::_slice])
    else:
        ax.set_xticks(_bin_centers)
        ax.set_xticklabels(xtick_labels)

    if x_axes_label:
        ax.set_xlabel(x_axes_label)

    # Flow extra styling
    if (fig := ax.figure) is None:
        raise ValueError("No figure found")
    if flow == "hint":
        _marker_size = (
            30
            * ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted()).width
        )
        if underflow > 0.0:
            ax.scatter(
                final_bins[0],
                0,
                _marker_size,
                marker=align_marker("<", halign="right"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.get_xaxis_transform(),
            )
        if overflow > 0.0:
            ax.scatter(
                final_bins[-1],
                0,
                _marker_size,
                marker=align_marker(">", halign="left"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.get_xaxis_transform(),
            )

    elif flow == "show" and (underflow > 0.0 or overflow > 0.0):
        xticks = ax.get_xticks().tolist()
        lw = ax.spines["bottom"].get_linewidth()
        _edges = plottables[0].edges
        _centers = plottables[0].centers
        _marker_size = (
            20
            * ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted()).width
        )
        if underflow > 0.0:
            xticks[0] = ""
            xticks[1] = f"<{flow_bins[2]}"
            ax.set_xticklabels(xticks)

            ax.plot(
                [_edges[0], _edges[1]],
                [0, 0],
                color="white",
                zorder=5,
                ls="--",
                lw=lw,
                transform=ax.get_xaxis_transform(),
                clip_on=False,
            )
            ax.scatter(
                _centers[0],
                0,
                _marker_size,
                marker=align_marker("d", valign="center"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.get_xaxis_transform(),
            )
        if overflow > 0.0:
            xticks[-1] = ""
            xticks[-2] = f">{flow_bins[-3]}"
            ax.set_xticklabels(xticks)
            ax.plot(
                [_edges[-2], _edges[-1]],
                [0, 0],
                color="white",
                zorder=5,
                ls="--",
                lw=lw,
                transform=ax.get_xaxis_transform(),
                clip_on=False,
            )
            ax.scatter(
                _centers[-1],
                0,
                _marker_size,
                marker=align_marker("d", valign="center"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.get_xaxis_transform(),
            )

    return return_artists


def hist2dplot(
    H,
    xbins=None,
    ybins=None,
    labels=None,
    cbar: bool = True,
    cbarsize="7%",
    cbarpad=0.2,
    cbarpos="right",
    cbarextend=True,
    cmin=None,
    cmax=None,
    ax: mpl.axes.Axes | None = None,
    flow="hint",
    **kwargs,
):
    """
    Create a 2D histogram plot from `np.histogram`-like inputs.

    Parameters
    ----------
    H : object
        Histogram object with containing values and optionally bins. Can be:

        - `np.histogram` tuple
        - `boost_histogram` histogram object
        - raw histogram values as list of list or 2d-array

    xbins : 1D array-like, optional, default None
        Histogram bins along x axis, if not part of ``H``.
    ybins : 1D array-like, optional, default None
        Histogram bins along y axis, if not part of ``H``.
    labels : 2D array (H-like) or bool, default None, optional
        Array of per-bin labels to display. If ``True`` will
        display numerical values
    cbar : bool, optional, default True
        Draw a colorbar. In contrast to mpl behaviors the cbar axes is
        appended in such a way that it doesn't modify the original axes
        width:height ratio.
    cbarsize : str or float, optional, default "7%"
        Colorbar width.
    cbarpad : float, optional, default 0.2
        Colorbar distance from main axis.
    cbarpos : {'right', 'left', 'bottom', 'top'}, optional,  default "right"
        Colorbar position w.r.t main axis.
    cbarextend : bool, optional, default False
        Extends figure size to keep original axes size same as without cbar.
        Only safe for 1 axes per fig.
    cmin : float, optional
        Colorbar minimum.
    cmax : float, optional
        Colorbar maximum.
    ax : matplotlib.axes.Axes, optional
        Axes object (if None, last one is fetched or one is created)
    flow :  str, optional {"show", "sum","hint", None}
            Whether plot the under/overflow bin. If "show", add additional under/overflow bin. If "sum", add the under/overflow bin content to first/last bin. "hint" would highlight the bins with under/overflow contents
    **kwargs :
        Keyword arguments passed to underlying matplotlib function - pcolormesh.

    Returns
    -------
        Hist2DArtist

    """

    # ax check
    if ax is None:
        ax = plt.gca()
    else:
        if not isinstance(ax, plt.Axes):
            raise ValueError("ax must be a matplotlib Axes object")

    h = hist_object_handler(H, xbins, ybins)

    # TODO: use Histogram everywhere

    H = np.copy(h.values())
    xbins, xtick_labels = get_plottable_protocol_bins(h.axes[0])
    ybins, ytick_labels = get_plottable_protocol_bins(h.axes[1])
    # Show under/overflow bins
    # "show": Add additional bin with 2 times bin width
    if (
        hasattr(h, "values")
        and "flow" not in inspect.getfullargspec(h.values).args
        and flow is not None
    ):
        print(
            f"Warning: {type(h)} is not allowed to get flow bins, flow bin option set to None"
        )
        flow = None
    elif flow in ["hint", "show"]:
        xwidth, ywidth = (xbins[-1] - xbins[0]) * 0.05, (ybins[-1] - ybins[0]) * 0.05
        pxbins = np.r_[xbins[0] - xwidth, xbins, xbins[-1] + xwidth]
        pybins = np.r_[ybins[0] - ywidth, ybins, ybins[-1] + ywidth]
        padded = to_padded2d(h)
        hint_xlo, hint_xhi, hint_ylo, hint_yhi = True, True, True, True
        if np.all(padded[0, :] == 0):
            padded = padded[1:, :]
            pxbins = pxbins[1:]
            hint_xlo = False
        if np.all(padded[-1, :] == 0):
            padded = padded[:-1, :]
            pxbins = pxbins[:-1]
            hint_xhi = False
        if np.all(padded[:, 0] == 0):
            padded = padded[:, 1:]
            pybins = pybins[1:]
            hint_ylo = False
        if np.all(padded[:, -1] == 0):
            padded = padded[:, :-1]
            pybins = pybins[:-1]
            hint_yhi = False
        if flow == "show":
            H = padded
            xbins, ybins = pxbins, pybins
    elif flow == "sum":
        H = np.copy(h.values())
        # Sum borders
        try:
            H[0], H[-1] = (
                H[0] + h.values(flow=True)[0, 1:-1],  # type: ignore[call-arg]
                H[-1] + h.values(flow=True)[-1, 1:-1],  # type: ignore[call-arg]
            )
            H[:, 0], H[:, -1] = (
                H[:, 0] + h.values(flow=True)[1:-1, 0],  # type: ignore[call-arg]
                H[:, -1] + h.values(flow=True)[1:-1, -1],  # type: ignore[call-arg]
            )
            # Sum corners to corners
            H[0, 0], H[-1, -1], H[0, -1], H[-1, 0] = (
                h.values(flow=True)[0, 0] + H[0, 0],  # type: ignore[call-arg]
                h.values(flow=True)[-1, -1] + H[-1, -1],  # type: ignore[call-arg]
                h.values(flow=True)[0, -1] + H[0, -1],  # type: ignore[call-arg]
                h.values(flow=True)[-1, 0] + H[-1, 0],  # type: ignore[call-arg]
            )
        except TypeError as error:
            if "got an unexpected keyword argument 'flow'" in str(error):
                raise TypeError(
                    f"The histograms value method {repr(h)} does not take a 'flow' argument. UHI Plottable doesn't require this to have, but it is required for this function."
                    f" Implementations like hist/boost-histogram support this argument."
                ) from error
    xbin_centers = xbins[1:] - np.diff(xbins) / float(2)
    ybin_centers = ybins[1:] - np.diff(ybins) / float(2)

    _x_axes_label = ax.get_xlabel()
    x_axes_label = (
        _x_axes_label if _x_axes_label != "" else get_histogram_axes_title(h.axes[0])
    )
    _y_axes_label = ax.get_ylabel()
    y_axes_label = (
        _y_axes_label if _y_axes_label != "" else get_histogram_axes_title(h.axes[1])
    )

    H = H.T

    if cmin is not None:
        H[H < cmin] = None
    if cmax is not None:
        H[H > cmax] = None

    X, Y = np.meshgrid(xbins, ybins)

    kwargs.setdefault("shading", "flat")
    pc = ax.pcolormesh(X, Y, H, vmin=cmin, vmax=cmax, **kwargs)

    if x_axes_label:
        ax.set_xlabel(x_axes_label)
    if y_axes_label:
        ax.set_ylabel(y_axes_label)

    ax.set_xlim(xbins[0], xbins[-1])
    ax.set_ylim(ybins[0], ybins[-1])

    if xtick_labels is None:  # Ordered axis
        if len(ax.get_xticks()) > len(xbins) * 0.7:
            ax.set_xticks(xbins)
    else:  # Categorical axis
        ax.set_xticks(xbin_centers)
        ax.set_xticklabels(xtick_labels)
    if ytick_labels is None:
        if len(ax.get_yticks()) > len(ybins) * 0.7:
            ax.set_yticks(ybins)
    else:  # Categorical axis
        ax.set_yticks(ybin_centers)
        ax.set_yticklabels(ytick_labels)

    if cbar:
        cax = append_axes(
            ax, size=cbarsize, pad=cbarpad, position=cbarpos, extend=cbarextend
        )
        cb_obj = plt.colorbar(pc, cax=cax)
    else:
        cb_obj = None

    plt.sca(ax)
    if flow == "show":
        if hint_xlo:
            ax.plot(
                [xbins[1]] * 2,
                [0, 1],
                ls="--",
                color="lightgrey",
                clip_on=False,
                transform=ax.get_xaxis_transform(),
            )
        if hint_xhi:
            ax.plot(
                [xbins[-2]] * 2,
                [0, 1],
                ls="--",
                color="lightgrey",
                clip_on=False,
                transform=ax.get_xaxis_transform(),
            )
        if hint_ylo:
            ax.plot(
                [0, 1],
                [ybins[1]] * 2,
                ls="--",
                color="lightgrey",
                clip_on=False,
                transform=ax.get_yaxis_transform(),
            )
        if hint_yhi:
            ax.plot(
                [0, 1],
                [ybins[-2]] * 2,
                ls="--",
                color="lightgrey",
                clip_on=False,
                transform=ax.get_yaxis_transform(),
            )
    elif flow == "hint":
        if (fig := ax.figure) is None:
            raise ValueError("No figure found.")
        _marker_size = (
            30
            * ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted()).width
        )
        if hint_xlo:
            ax.scatter(
                0,
                0,
                _marker_size,
                marker=align_marker("<", halign="right", valign="bottom"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.transAxes,
            )
        if hint_xhi:
            ax.scatter(
                1,
                0,
                _marker_size,
                marker=align_marker(">", halign="left"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.transAxes,
            )
        if hint_ylo:
            ax.scatter(
                0,
                0,
                _marker_size,
                marker=align_marker("v", valign="top", halign="left"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.transAxes,
            )
        if hint_yhi:
            ax.scatter(
                0,
                1,
                _marker_size,
                marker=align_marker("^", valign="bottom"),
                edgecolor="black",
                zorder=5,
                clip_on=False,
                facecolor="white",
                transform=ax.transAxes,
            )

    _labels: np.ndarray | None = None
    if isinstance(labels, bool):
        _labels = H if labels else None
    elif np.iterable(labels):
        label_array = np.asarray(labels).T
        if H.shape == label_array.shape:
            _labels = label_array
        else:
            raise ValueError(
                f"Labels input has incorrect shape (expect: {H.shape}, got: {label_array.shape})"
            )
    elif labels is not None:
        raise ValueError(
            "Labels not understood, either specify a bool or a Hist-like array"
        )

    text_artists = []
    if _labels is not None:
        if (pccmap := pc.cmap) is None:
            raise ValueError("No colormap found.")
        for ix, xc in enumerate(xbin_centers):
            for iy, yc in enumerate(ybin_centers):
                normedh = pc.norm(H[iy, ix])
                color = "black" if isLight(pccmap(normedh)[:-1]) else "lightgrey"
                text_artists.append(
                    ax.text(
                        xc, yc, _labels[iy, ix], ha="center", va="center", color=color
                    )
                )

    return ColormeshArtists(pc, cb_obj, text_artists)


#############################################
# Utils
def overlap(ax, bbox, get_vertices=False):
    """
    Find overlap of bbox for drawn elements an axes.
    """
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, Rectangle
    from matplotlib.text import Text

    # From
    # https://github.com/matplotlib/matplotlib/blob/08008d5cb4d1f27692e9aead9a76396adc8f0b19/lib/matplotlib/legend.py#L845
    lines = []
    bboxes = []
    for handle in ax.lines:
        assert isinstance(handle, Line2D)
        path = handle.get_path()
        lines.append(path)
    for handle in ax.collections:
        for path in handle.get_paths():
            lines.append(path.interpolated(20))

    for handle in ax.patches:
        assert isinstance(handle, Patch)
        if isinstance(handle, Rectangle):
            transform = handle.get_data_transform()
            bboxes.append(handle.get_bbox().transformed(transform))
        else:
            if len(handle.get_path().vertices) == 0:
                continue
            lines.append(handle.get_path().interpolated(20))

    for handle in ax.texts:
        assert isinstance(handle, Text)
        bboxes.append(handle.get_window_extent())

    # TODO Possibly other objects

    vertices = np.concatenate([line.vertices for line in lines])
    tvertices = [ax.transData.transform(v) for v in vertices]

    overlap = bbox.count_contains(tvertices) + bbox.count_overlaps(bboxes)

    if get_vertices:
        return overlap, vertices
    else:
        return overlap


def _draw_leg_bbox(ax):
    """
    Draw legend() and fetch it's bbox
    """
    fig = ax.figure
    leg = ax.get_legend()
    if leg is None:
        leg = [
            c for c in ax.get_children() if isinstance(c, plt.matplotlib.legend.Legend)
        ][0]

    fig.canvas.draw()
    return leg.get_frame().get_bbox()


def _draw_text_bbox(ax):
    """
    Draw legend() and fetch it's bbox
    """
    fig = ax.figure
    textboxes = [k for k in ax.get_children() if isinstance(k, AnchoredText)]
    fig.canvas.draw()
    if len(textboxes) > 1:
        logging.warning("More than one textbox found")
        for box in textboxes:
            if box.loc in [1, 2]:
                bbox = box.get_tightbbox(fig.canvas.renderer)
    else:
        bbox = textboxes[0].get_tightbbox(fig.canvas.renderer)

    return bbox


def yscale_legend(
    ax: mpl.axes.Axes | None = None,
    otol: float | int | None = None,
    soft_fail: bool = False,
) -> mpl.axes.Axes:
    """
    Automatically scale y-axis up to fit in legend().

    Parameters
    ----------
        ax : matplotlib.axes.Axes, optional
            Axes object (if None, last one is fetched or one is created)
        otol : float, optional
            Tolerance for overlap, default 0. Set ``otol > 0`` for less strict scaling.
        soft_fail : bool, optional
            Set ``soft_fail=True`` to return even if it could not fit the legend.

    Returns
    -------
        ax : matplotlib.axes.Axes
    """
    if ax is None:
        ax = plt.gca()
    if otol is None:
        otol = 0

    scale_factor = 10 ** (1.05) if ax.get_yscale() == "log" else 1.05
    max_scales = 0
    while overlap(ax, _draw_leg_bbox(ax)) > otol:
        logging.debug(
            f"Legend overlap with other artists is {overlap(ax, _draw_leg_bbox(ax))}."
        )
        logging.info("Scaling y-axis by 5% to fit legend")
        ax.set_ylim(ax.get_ylim()[0], ax.get_ylim()[-1] * scale_factor)
        if (fig := ax.figure) is None:
            raise RuntimeError("Could not fetch figure, maybe no plot is drawn yet?")
        fig.canvas.draw()
        if max_scales > 10:
            if not soft_fail:
                raise RuntimeError(
                    "Could not fit legend in 10 iterations, return anyway by passing `soft_fail=True`."
                )
            else:
                logging.warning("Could not fit legend in 10 iterations")
                break
        max_scales += 1
    return ax


def yscale_anchored_text(
    ax: mpl.axes.Axes | None = None,
    otol: float | int | None = None,
    soft_fail: bool = False,
) -> mpl.axes.Axes:
    """
    Automatically scale y-axis up to fit AnchoredText

    Parameters
    ----------
        ax : matplotlib.axes.Axes, optional
            Axes object (if None, last one is fetched or one is created)
        otol : float, optional
            Tolerance for overlap, default 0. Set ``otol > 0`` for less strict scaling.
        soft_fail : bool, optional
            Set ``soft_fail=True`` to return even if it could not fit the legend.

    Returns
    -------
        ax : matplotlib.axes.Axes
    """
    if ax is None:
        ax = plt.gca()
    if otol is None:
        otol = 0

    scale_factor = 10 ** (1.05) if ax.get_yscale() == "log" else 1.05
    max_scales = 0
    while overlap(ax, _draw_text_bbox(ax)) > otol:
        logging.debug(
            f"AnchoredText overlap with other artists is {overlap(ax, _draw_text_bbox(ax))}."
        )
        logging.info("Scaling y-axis by 5% to fit legend")
        ax.set_ylim(ax.get_ylim()[0], ax.get_ylim()[-1] * scale_factor)
        if (fig := ax.figure) is None:
            raise RuntimeError("Could not fetch figure, maybe no plot is drawn yet?")
        fig.canvas.draw()
        if max_scales > 10:
            if not soft_fail:
                raise RuntimeError(
                    "Could not fit AnchoredText in 10 iterations, return anyway by passing `soft_fail=True`."
                )
            else:
                logging.warning("Could not fit AnchoredText in 10 iterations")
                break
        max_scales += 1
    return ax


def ylow(ax: mpl.axes.Axes | None = None, ylow: float | None = None) -> mpl.axes.Axes:
    """
    Set lower y limit to 0 or a specific value if not data/errors go lower.

    Parameters
    ----------
        ax : matplotlib.axes.Axes, optional
            Axes object (if None, last one is fetched or one is created)
        ylow : float, optional
            Set lower y limit to a specific value.

    Returns
    -------
        ax : matplotlib.axes.Axes
    """
    if ax is None:
        ax = plt.gca()

    if ax.get_yaxis().get_scale() == "log":
        return ax

    if ylow is None:
        # Check full figsize below 0
        bbox = Bbox.from_bounds(
            0, 0, ax.get_window_extent().width, -ax.get_window_extent().height
        )
        if overlap(ax, bbox) == 0:
            ax.set_ylim(0, None)
        else:
            ydata = overlap(ax, bbox, get_vertices=True)[1][:, 1]
            ax.set_ylim(np.min([np.min(ydata), ax.get_ylim()[0]]), None)

    else:
        ax.set_ylim(0, ax.get_ylim()[-1])

    return ax


def mpl_magic(ax=None, info=True):
    """
    Consolidate all ex-post style adjustments:
        ylow
        yscale_legend
    """
    if ax is None:
        ax = plt.gca()
    if info:
        print("Running ROOT/CMS style adjustments (hide with info=False):")

    ax = ylow(ax)
    ax = yscale_legend(ax)
    ax = yscale_anchored_text(ax)

    return ax


########################################
# Figure/axes helpers
def rescale_to_axessize(ax, w, h):
    """
    Adjust figure size to axes size in inches
    Parameters: w, h: width, height in inches
    """
    if not ax:
        ax = plt.gca()
    left = ax.figure.subplotpars.left
    r = ax.figure.subplotpars.right
    t = ax.figure.subplotpars.top
    b = ax.figure.subplotpars.bottom
    figw = float(w) / (r - left)
    figh = float(h) / (t - b)
    ax.figure.set_size_inches(figw, figh)


def box_aspect(ax, aspect=1):
    """
    Adjust figure size to axes size in inches
    Parameters: aspect: float, optional aspect ratio

    """
    position = ax.get_position()

    fig_width, fig_height = ax.get_figure().get_size_inches()
    fig_aspect = fig_height / fig_width

    pb = position.frozen()
    pb1 = pb.shrunk_to_aspect(aspect, pb, fig_aspect)
    ax.set_position(pb1)


class RemainderFixed(axes_size.Scaled):
    def __init__(self, xsizes, ysizes, divider):
        self.xsizes = xsizes
        self.ysizes = ysizes
        self.div = divider

    def get_size(self, renderer):
        xrel, xabs = sum(self.xsizes, start=axes_size.Fixed(0)).get_size(renderer)
        yrel, yabs = sum(self.ysizes, start=axes_size.Fixed(0)).get_size(renderer)
        bb = Bbox.from_bounds(*self.div.get_position()).transformed(
            self.div._fig.transFigure
        )
        w = bb.width / self.div._fig.dpi - xabs
        h = bb.height / self.div._fig.dpi - yabs
        return 0, min([w, h])


def make_square_add_cbar(ax, size=0.4, pad=0.1):
    """
    Make input axes square and return an appended axes to the right for
    a colorbar. Both axes resize together to fit figure automatically.
    Works with tight_layout().
    """
    divider = make_axes_locatable(ax)

    margin_size = axes_size.Fixed(size)
    pad_size = axes_size.Fixed(pad)
    xsizes = [pad_size, margin_size]
    ysizes = xsizes

    cax = divider.append_axes("right", size=margin_size, pad=pad_size)

    divider.set_horizontal([RemainderFixed(xsizes, ysizes, divider)] + xsizes)
    divider.set_vertical([RemainderFixed(xsizes, ysizes, divider)] + ysizes)
    return cax


def append_axes(ax, size=0.1, pad=0.1, position="right", extend=False):
    """
    Append a side ax to the current figure and return it.
    Figure is automatically extended along the direction of the added axes to
    accommodate it. Unfortunately can not be reliably chained.
    """
    fig = ax.figure
    bbox = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    width, height = bbox.width, bbox.height

    def convert(fraction, position=position):
        if isinstance(fraction, str) and fraction.endswith("%"):
            if position in ["right", "left"]:
                fraction = width * float(fraction.strip("%")) / 100
            elif position in ["top", "bottom"]:
                fraction = height * float(fraction.strip("%")) / 100
        return fraction

    size = convert(size)
    pad = convert(pad)

    divider = make_axes_locatable(ax)
    margin_size = axes_size.Fixed(size)
    pad_size = axes_size.Fixed(pad)
    xsizes = [pad_size, margin_size]
    if position in ["top", "bottom"]:
        xsizes = xsizes[::-1]
    yhax = divider.append_axes(position, size=margin_size, pad=pad_size)

    if extend:

        def extend_ratio(ax, yhax):
            ax.figure.canvas.draw()
            orig_size = ax.get_position().size
            new_size = sum(itax.get_position().size for itax in [ax, yhax])
            return new_size / orig_size

        if position in ["right"]:
            divider.set_horizontal([axes_size.Fixed(width)] + xsizes)
            fig.set_size_inches(
                fig.get_size_inches()[0] * extend_ratio(ax, yhax)[0],
                fig.get_size_inches()[1],
            )
        elif position in ["left"]:
            divider.set_horizontal(xsizes[::-1] + [axes_size.Fixed(width)])
            fig.set_size_inches(
                fig.get_size_inches()[0] * extend_ratio(ax, yhax)[0],
                fig.get_size_inches()[1],
            )
        elif position in ["top"]:
            divider.set_vertical([axes_size.Fixed(height)] + xsizes[::-1])
            fig.set_size_inches(
                fig.get_size_inches()[0],
                fig.get_size_inches()[1] * extend_ratio(ax, yhax)[1],
            )
            ax.get_shared_x_axes().join(ax, yhax)
        elif position in ["bottom"]:
            divider.set_vertical(xsizes + [axes_size.Fixed(height)])
            fig.set_size_inches(
                fig.get_size_inches()[0],
                fig.get_size_inches()[1] * extend_ratio(ax, yhax)[1],
            )
            ax.get_shared_x_axes().join(ax, yhax)

    return yhax


####################
# Legend Helpers
def hist_legend(ax=None, **kwargs):
    from matplotlib.lines import Line2D

    if ax is None:
        ax = plt.gca()

    handles, labels = ax.get_legend_handles_labels()
    new_handles = [
        Line2D([], [], c=h.get_edgecolor()) if isinstance(h, mpl.patches.Polygon) else h
        for h in handles
    ]
    ax.legend(handles=new_handles[::-1], labels=labels[::-1], **kwargs)

    return ax


def sort_legend(ax, order=None):
    """
    ax : axes with legend labels in it
    order : Ordered dict with renames or array with order
    """

    handles, labels = ax.get_legend_handles_labels()
    by_label = OrderedDict(zip(labels, handles))

    if isinstance(order, OrderedDict):
        ordered_label_list = list(order.keys())
    elif isinstance(order, (list, tuple, np.ndarray)):
        ordered_label_list = list(order)
    elif order is None:
        ordered_label_list = labels
    else:
        raise TypeError(f"Unexpected values type of order: {type(order)}")

    ordered_label_list = [entry for entry in ordered_label_list if entry in labels]
    ordered_label_values = [by_label[k] for k in ordered_label_list]
    if isinstance(order, OrderedDict):
        ordered_label_list = [order[k] for k in ordered_label_list]
    return ordered_label_values, ordered_label_list
