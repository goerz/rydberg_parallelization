"""Code for splitting up the Hamiltonian into blocks"""
from qutip import Qobj
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
