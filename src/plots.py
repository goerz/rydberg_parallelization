"""Plotting routines"""
import matplotlib.pylab as plt
import numpy as np


def show_spy(
    m,
    outfile=None,
    figsize=None,
    show_diagonal=False,
    grid=False,
    xlim=None,
    ylim=None,
    label_pixels=False,
    markersize=1,
):
    """Show the sparsity pattern of the given sparse matrix"""
    if figsize is None:
        fig, ax = plt.subplots()
    else:
        fig, ax = plt.subplots(figsize=figsize)
    ax.spy(m, markersize=markersize)
    diag_vals = np.linspace(0, m.shape[0], 10)
    if show_diagonal:
        ax.plot(diag_vals, diag_vals, color="black")
    if label_pixels:
        ax.set_xticks(list(range(0, m.shape[0])))
        ax.set_yticks(list(reversed(range(0, m.shape[1]))))
    if xlim is not None:
        ax.set_xlim(xlim)
    if ylim is not None:
        ax.set_ylim(ylim)
    if grid:
        ax.grid()
    if outfile is None:
        fig.show()
    else:
        fig.savefig(outfile)
        plt.close()


def plot_population(pop_data, pop_data_baseline=None, alpha=1.0):
    """Plot the population

    The `pop_data` is obtained as e.g.

        pop_data = np.genfromtxt((Path(RF) / 'population.dat')).transpose()
    """
    fig, ax = plt.subplots()
    l1, = ax.plot(pop_data[0], pop_data[1], label="initial", alpha=alpha)
    l2, = ax.plot(pop_data[0], pop_data[2], label="target", alpha=alpha)
    l3, = ax.plot(
        pop_data[0], pop_data[1] + pop_data[2], label="subspace", alpha=alpha
    )
    if pop_data_baseline is not None:
        ax.plot(
            pop_data_baseline[0],
            pop_data_baseline[1],
            ls="--",
            color=l1.get_color(),
        )
        ax.plot(
            pop_data_baseline[0],
            pop_data_baseline[2],
            ls="--",
            color=l2.get_color(),
        )
        ax.plot(
            pop_data_baseline[0],
            pop_data_baseline[1] + pop_data_baseline[2],
            ls="--",
            color=l3.get_color(),
        )

    ax.legend()
    fig.show()
