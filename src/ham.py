"""Rydberg Hamiltonian"""
import qutip

from .qdyn.io import read_indexed_matrix

def rydberg_hamiltonian(
    n_hilbert,
    H_drift_file,
    H_sigma_file,
    H_pi_file,
    F_DC,
    Omega_sigma,
):
    """Construct the time-dependent Hamiltonian

    Args:
        H_drift_file (str)
        H_sigma_file (str)
        H_pi_file (str)
        F_DC (float)
        Omega_sig (Pulse)
        exapnd_H_sigma (bool)
    """
    H_drift = qutip.Qobj(
        read_indexed_matrix(
            H_drift_file, shape=(n_hilbert, n_hilbert), expand_hermitian=False
        )
    )
    H_sigma = qutip.Qobj(
        read_indexed_matrix(
            H_sigma_file,
            shape=(n_hilbert, n_hilbert),
            expand_hermitian=False,
        )
    )
    H_pi = qutip.Qobj(
        read_indexed_matrix(
            H_pi_file, shape=(n_hilbert, n_hilbert), expand_hermitian=False
        )
    )
    H_0 = H_drift + F_DC * H_pi
    return [H_0, [H_sigma, Omega_sigma], [H_sigma.dag(), Omega_sigma]]
