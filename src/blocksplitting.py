"""Code for splitting up the Hamiltonian into blocks"""
from qutip import Qobj, qdiags
import numpy as np
import scipy.sparse


def split_AB(m, block_info, n, as_qobj=False):
    """Split a sparse matrix into two block-diagonal matrices

    Args:
        m (scipy.sparse.spmatrix or qutip.Qobj): The sparse matrix to operate
            on
        block_info (np.ndarray): Array of block size information
        n (int): dimension of `m`

    Returns:
        tuple[scipy.sparse.csr_matrix]: a tuple of two sparse matrices, where
        each matrix is block-diagonal

    Assumptions on `m`:
        - contains data only in the upper triangle
        - consists of rectangular blocks sitting on the diagonal

    The size of the rectangular blocks is specified in the `block_info` array
    (0 values in that array are discarded). The values in `block_info` is the
    number of rows in each consecutive block.

    The matrix is split in such a way that alternating blocks are separated
    into alternating result matrices. This guarantees that the two resulting
    matrices are block diagonal (with only the upper-left quadrant of each
    block non-zero)
    """
    if isinstance(m, Qobj):
        m = m.data
    assert (scipy.sparse.triu(m) - m).nnz == 0
    m_coo = m.tocoo()
    assert np.all(np.sort(m_coo.row) == m_coo.row)
    assert block_info.dtype == np.int32
    data, row, col, nnz = m_coo.data, m_coo.row, m_coo.col, m_coo.nnz
    block_info = iter([n for n in block_info if n != 0])
    selector = 1
    n_row_limit = next(block_info) - 1  # how far can we look ahead?
    AB = {
        0: {'data': [], 'row': [], 'col': []},
        1: {'data': [], 'row': [], 'col': []},
    }
    for k in range(nnz):
        i = row[k]
        j = col[k]
        v = data[k]
        if i > n_row_limit:
            n_row_limit += next(block_info)
            selector = (selector + 1) % 2  # toggle 0↔1
        AB[selector]['data'].append(v)
        AB[selector]['row'].append(i)
        AB[selector]['col'].append(j)
    res = tuple(
        scipy.sparse.coo_matrix(
            (np.array(d['data']), (np.array(d['row']), np.array(d['col']))),
            shape=(n, n),
        ).tocsr()
        for d in [AB[0], AB[1]]
    )
    if as_qobj:
        res = Qobj(res[0]), Qobj(res[1])
    return res


def split_AB_blocks(m, block_info, n, as_qobj=False):
    """Split a sparse matrix into two block-diagonal matrices

    Args:
        m (scipy.sparse.spmatrix): The sparse matrix to operator on
        block_info (np.ndarray): Array of block size information
        n (int): dimension of `m`

    Returns:
        tuple[list[scipy.sparse.csr_matrix]]: a tuple of two "buckets" A, B
        ...

    Assumptions on `m`:
        - contains data only in the upper triangle
        - consists of rectangular blocks sitting on the diagonal

    TODO
    """
    if isinstance(m, Qobj):
        m = m.data
    assert (scipy.sparse.triu(m) - m).nnz == 0
    m_coo = m.tocoo()
    assert np.all(np.sort(m_coo.row) == m_coo.row)
    assert block_info.dtype == np.int32
    data, row, col, nnz = m_coo.data, m_coo.row, m_coo.col, m_coo.nnz
    block_info = iter([n for n in block_info if n != 0])
    selector = 0
    n_row_limit = next(block_info) - 1  # how far can we look ahead?
    AB = {0: [], 1: []}
    block_data, block_row, block_col = [], [], []
    for k in range(nnz):
        i = row[k]
        j = col[k]
        v = data[k]
        if i > n_row_limit:
            n_row_limit += next(block_info)
            selector = (selector + 1) % 2  # toggle 0↔1
            if len(block_data) > 0:
                block = scipy.sparse.coo_matrix(
                    (block_data, (block_row, block_col)), shape=(n, n)
                )
                if as_qobj:
                    block = Qobj(block)
                AB[selector].append(block)
                block_data, block_row, block_col = [], [], []
        block_data.append(v)
        block_row.append(i)
        block_col.append(j)
    if len(block_data) > 0:
        selector = (selector + 1) % 2  # toggle 0↔1
        block = scipy.sparse.coo_matrix(
            (block_data, (block_row, block_col)), shape=(n, n)
        )
        if as_qobj:
            block = Qobj(block)
        AB[selector].append(block)
    return AB[0], AB[1]


def split_list(lst, n):
    """Split the given `lst` into `n` chunks"""
    splitted = []
    for i in reversed(range(1, n + 1)):
        split_point = len(lst) // i
        splitted.append(lst[:split_point])
        lst = lst[split_point:]
    return splitted


def split_diagonal_hamiltonian(H0, n_blocks):
    """Split a diagonal Hamiltonian into blocks"""
    diag_data = H0.diag()
    assert (H0 - qdiags(diag_data, 0)).data.nnz == 0
    res = []
    for i in range(n_blocks):
        res.append(diag_data.copy())
        for j in range(n_blocks):
            if i == j:
                continue
            res[-1][split_list(range(len(diag_data)), n_blocks)[j]] = 0
        res[-1] = qdiags(res[-1], 0)
    return res


def balance_threads(thread_bins, thread_filled, i, j):
    assert i < j
    avg = 0.5 * (thread_filled[i] + thread_filled[j])
    if thread_filled[i] > thread_filled[j]:
        while thread_filled[i] > avg:
            block = thread_bins[i].pop()
            thread_bins[j].insert(0, block)
            thread_filled[i] -= block.data.nnz
            thread_filled[j] += block.data.nnz
    elif thread_filled[i] < thread_filled[j]:
        while thread_filled[i] < avg:
            block = thread_bins[j].pop(0)
            thread_bins[i].append(block)
            thread_filled[j] -= block.data.nnz
            thread_filled[i] += block.data.nnz


def distribute_keep_together(list_of_blocks, n_threads):
    total_nnz = sum([block.data.nnz for block in list_of_blocks])
    nnz_per_thread = total_nnz / n_threads
    thread_bins = [[] for _ in range(n_threads)]
    thread_filled = np.array([0 for _ in range(n_threads)])
    i_thread = 0
    for block in list_of_blocks:
        if (thread_filled[i_thread] + 0.5 * block.data.nnz) >= nnz_per_thread:
            i_thread = min(i_thread + 1, n_threads - 1)
        thread_bins[i_thread].append(block)
        thread_filled[i_thread] += block.data.nnz
    sigma0 = (
        np.sum(np.abs(np.array(thread_filled) - nnz_per_thread))
    ) / n_threads
    while True:
        thread_bins_prev = thread_bins.copy()
        thread_filled_prev = thread_filled.copy()
        for i_thread in reversed(range(1, n_threads)):
            balance_threads(thread_bins, thread_filled, i_thread - 1, i_thread)
        sigma = (
            np.sum(np.abs(np.array(thread_filled) - nnz_per_thread))
        ) / n_threads
        for i_thread in range(1, n_threads):
            balance_threads(thread_bins, thread_filled, i_thread - 1, i_thread)
        sigma = (
            np.sum(np.abs(np.array(thread_filled) - nnz_per_thread))
        ) / n_threads
        if sigma >= sigma0:
            thread_bins = thread_bins_prev
            thread_filled = thread_filled_prev
            break
        else:
            sigma0 = sigma
    return thread_bins
