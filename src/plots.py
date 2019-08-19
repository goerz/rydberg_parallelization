import matplotlib.pylab as plt
import numpy as np

def show_spy(m, outfile=None, show_diagonal=False):
    fig, ax = plt.subplots()
    ax.spy(m, markersize=1)
    diag_vals = np.linspace(0, m.shape[0], 10)
    if show_diagonal:
        ax.plot(diag_vals, diag_vals, color='black')
    if outfile is None:
        plt.show(fig)
    else:
        fig.savefig(outfile)
        plt.close()
