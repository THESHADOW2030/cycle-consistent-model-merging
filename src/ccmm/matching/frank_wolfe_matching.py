import logging
from functools import partial
from typing import Dict, List, Tuple

import numpy as np
import torch
from pytorch_lightning import seed_everything
from scipy.optimize import fminbound
from tqdm import tqdm

from ccmm.matching.permutation_spec import PermutationSpec
from ccmm.matching.utils import PermutationIndices, PermutationMatrix
from ccmm.matching.weight_matching import solve_linear_assignment_problem
from ccmm.utils.utils import ModelParams

pylogger = logging.getLogger(__name__)


def frank_wolfe_weight_matching(
    ps: PermutationSpec,
    fixed: ModelParams,
    permutee: ModelParams,
    initialization_method: str,
    max_iter=100,
    return_perm_history=False,
    num_trials=3,
):
    """
    Find a permutation of params_b to make them match params_a.

    :param ps: PermutationSpec
    :param fixed: the parameters to match
    :param permutee: the parameters to permute
    """
    params_a, params_b = fixed, permutee

    # For a MLP of 4 layers it would be something like {'P_0': 512, 'P_1': 512, 'P_2': 512, 'P_3': 256}. Input and output dim are never permuted.
    perm_sizes = {}

    # FOR MLP
    # ps.perm_to_layers_and_axes["P_4"] = [("layer4.weight", 0)]

    # FOR RESNET
    ps.perm_to_layers_and_axes["P_final"] = [("linear.weight", 0)]

    for perm_name, params_and_axes in ps.perm_to_layers_and_axes.items():
        # params_and_axes is a list of tuples, e.g. [('layer_0.weight', 0), ('layer_0.bias', 0), ('layer_1.weight', 0)..]
        relevant_params, relevant_axis = params_and_axes[0]
        param_shape = params_a[relevant_params].shape
        perm_sizes[perm_name] = param_shape[relevant_axis]

    seeds = np.random.randint(0, 1000, num_trials)
    best_obj = 0.0

    for seed in tqdm(seeds, desc="Running multiple trials"):
        seed_everything(seed)

        perm_matrices, perm_matrices_history, trial_obj = frank_wolfe_weight_matching_trial(
            params_a, params_b, perm_sizes, initialization_method, ps, max_iter
        )
        pylogger.info(f"Trial objective: {trial_obj}")

        if trial_obj > best_obj:
            pylogger.info(f"New best objective! Previous was {best_obj}")
            best_obj = trial_obj
            best_perm_matrices = perm_matrices
            best_perm_matrices_history = perm_matrices_history

    all_perm_indices = {p: solve_linear_assignment_problem(perm) for p, perm in best_perm_matrices.items()}

    if return_perm_history:
        return all_perm_indices, best_perm_matrices_history
    else:
        return all_perm_indices


def frank_wolfe_weight_matching_trial(params_a, params_b, perm_sizes, initialization_method, perm_spec, max_iter=100):

    perm_matrices: Dict[str, PermutationMatrix] = initialize_perm_matrices(perm_sizes, initialization_method)
    perm_matrices_history = [perm_matrices]

    old_obj = 0.0
    patience_steps = 0

    for iteration in tqdm(range(max_iter), desc="Weight matching"):
        pylogger.debug(f"Iteration {iteration}")

        gradients = weight_matching_gradient_fn_layerwise(
            params_a, params_b, perm_matrices, perm_spec.layer_and_axes_to_perm, perm_sizes
        )

        proj_grads = project_gradients(gradients)

        line_search_step_func = partial(
            line_search_step,
            proj_grads=proj_grads,
            perm_matrices=perm_matrices,
            params_a=params_a,
            params_b=params_b,
            layers_and_axes_to_perms=perm_spec.layer_and_axes_to_perm,
        )

        step_size = fminbound(line_search_step_func, 0, 1)
        pylogger.debug(f"Step size: {step_size}")

        perm_matrices = update_perm_matrices(perm_matrices, proj_grads, step_size)

        new_obj = get_global_obj_layerwise(params_a, params_b, perm_matrices, perm_spec.layer_and_axes_to_perm)

        pylogger.debug(f"Objective: {np.round(new_obj, 6)}")

        if (new_obj - old_obj) < 1e-6:
            patience_steps += 1
        else:
            patience_steps = 0
            old_obj = new_obj

        if patience_steps >= 15:
            break

        perm_matrices_history.append(perm_matrices)

    return perm_matrices, perm_matrices_history, new_obj


def initialize_perm_matrices(perm_sizes, initialization_method):
    if initialization_method == "identity":
        return {p: torch.eye(n) for p, n in perm_sizes.items()}
    elif initialization_method == "random":
        return {p: torch.rand(n, n) for p, n in perm_sizes.items()}
    elif initialization_method == "sinkhorn":
        return {p: sinkhorn_knopp(torch.rand(n, n)) for p, n in perm_sizes.items()}
    else:
        raise ValueError(f"Unknown initialization method {initialization_method}")


def project_gradients(gradients):
    proj_grads = {}

    for perm_name, grad in gradients.items():

        proj_grad = solve_linear_assignment_problem(grad, return_matrix=True)

        proj_grads[perm_name] = proj_grad

    return proj_grads


def line_search_step(
    t: float,
    params_a,
    params_b,
    proj_grads: Dict[str, torch.Tensor],
    perm_matrices: Dict[str, PermutationIndices],
    layers_and_axes_to_perms,
):

    interpolated_perms = {}

    for perm_name, perm in perm_matrices.items():
        proj_grad = proj_grads[perm_name]

        if perm_name in {"P_final", "P_4"}:
            interpolated_perms[perm_name] = perm
            continue

        interpolated_perms[perm_name] = (1 - t) * perm + t * proj_grad

    tot_obj = get_global_obj_layerwise(params_a, params_b, interpolated_perms, layers_and_axes_to_perms)

    return -tot_obj


def get_global_obj_layerwise(params_a, params_b, perm_matrices, layers_and_axes_to_perms):

    tot_obj = 0.0

    for layer, axes_and_perms in layers_and_axes_to_perms.items():
        assert layer in params_a.keys()
        assert layer in params_b.keys()

        Wa, Wb = params_a[layer], params_b[layer]
        if Wa.dim() == 1:
            Wa = Wa.unsqueeze(1)
            Wb = Wb.unsqueeze(1)

        row_perm_id = axes_and_perms[0]
        assert row_perm_id is None or row_perm_id in perm_matrices.keys()
        row_perm = perm_matrices[row_perm_id] if row_perm_id is not None else torch.eye(Wa.shape[0])

        col_perm_id = axes_and_perms[1] if len(axes_and_perms) > 1 else None
        assert col_perm_id is None or col_perm_id in perm_matrices.keys()
        col_perm = perm_matrices[col_perm_id] if col_perm_id is not None else torch.eye(Wa.shape[1])

        layer_similarity = compute_layer_similarity(Wa, Wb, row_perm, col_perm)

        tot_obj += layer_similarity

    return tot_obj


def weight_matching_gradient_fn_layerwise(params_a, params_b, perm_matrices, layers_and_axes_to_perms, perm_sizes):
    """
    Compute gradient of the weight matching objective function w.r.t. P_curr and P_prev.
    sim = <Wa_i, Pi Wb_i P_{i-1}^T>_f where f is the Frobenius norm, rewrite it as < A, xBy^T>_f where A = Wa_i, x = Pi, B = Wb_i, y = P_{i-1}

    Returns:
        grad_P_curr: Gradient of objective function w.r.t. P_curr.
        grad_P_prev: Gradient of objective function w.r.t. P_prev.
    """
    gradients = {p: torch.zeros((perm_sizes[p], perm_sizes[p])) for p in perm_matrices.keys()}

    for layer, axes_and_perms in layers_and_axes_to_perms.items():
        assert layer in params_a.keys()
        assert layer in params_b.keys()

        Wa, Wb = params_a[layer], params_b[layer]
        if Wa.dim() == 1:
            Wa = Wa.unsqueeze(1)
            Wb = Wb.unsqueeze(1)

        row_perm_id = axes_and_perms[0]
        assert row_perm_id is None or row_perm_id in perm_matrices.keys()
        row_perm = perm_matrices[row_perm_id] if row_perm_id is not None else torch.eye(Wa.shape[0])

        col_perm_id = axes_and_perms[1] if len(axes_and_perms) > 1 else None
        assert col_perm_id is None or col_perm_id in perm_matrices.keys()
        col_perm = perm_matrices[col_perm_id] if col_perm_id is not None else torch.eye(Wa.shape[1])

        grad_P_curr = compute_gradient_P_curr(Wa, Wb, col_perm)
        grad_P_prev = compute_gradient_P_prev(Wa, Wb, row_perm)

        if row_perm_id:
            gradients[row_perm_id] += grad_P_curr
        if col_perm_id:
            gradients[col_perm_id] += grad_P_prev

    return gradients


def weight_matching_gradient_fn(params_a, params_b, P_curr, P_curr_name, perm_to_axes, perm_matrices, gradients):
    """
    Compute gradient of the weight matching objective function w.r.t. P_curr and P_prev.
    sim = <Wa_i, Pi Wb_i P_{i-1}^T>_f where f is the Frobenius norm, rewrite it as < A, xBy^T>_f where A = Wa_i, x = Pi, B = Wb_i, y = P_{i-1}

    Returns:
        grad_P_curr: Gradient of objective function w.r.t. P_curr.
        grad_P_prev: Gradient of objective function w.r.t. P_prev.
    """

    # all the params that are permuted by this permutation matrix, together with the axis on which it acts
    # e.g. ('layer_0.weight', 0), ('layer_0.bias', 0), ('layer_1.weight', 0)..
    params_and_axes: List[Tuple[str, int]] = perm_to_axes[P_curr_name]

    P_prev_name = None

    for params_name, axis in params_and_axes:

        # axis != 0 will be considered when P_curr will be P_prev for some next layer
        if axis == 0:

            Wa, Wb = params_a[params_name], params_b[params_name]
            assert Wa.shape == Wa.shape

            if Wa.dim() == 1:
                Wa = Wa.unsqueeze(1)
                Wb = Wb.unsqueeze(1)

            P_prev_name, P_prev = get_prev_permutation(params_name, perm_to_axes, perm_matrices)

            if not is_last_layer(params_and_axes):

                grad_P_curr = compute_gradient_P_curr(Wa, Wb, P_prev)

                gradients[P_curr_name] += grad_P_curr

            if P_prev_name is not None:
                grad_P_prev = compute_gradient_P_prev(Wa, Wb, P_curr)

                gradients[P_prev_name] += grad_P_prev


def is_last_layer(params_and_axes):
    return len(params_and_axes) == 1


def compute_single_perm_obj_function(params_a, params_b, P_curr, P_curr_name, perm_to_axes, perm_matrices, debug=True):
    """
    Compute gradient of the weight matching objective function w.r.t. P_curr and P_prev.
    sim = <Wa_i, Pi Wb_i P_{i-1}^T>_f where f is the Frobenius norm, rewrite it as < A, xBy^T>_f where A = Wa_i, x = Pi, B = Wb_i, y = P_{i-1}

    Returns:
        grad_P_curr: Gradient of objective function w.r.t. P_curr.
        grad_P_prev: Gradient of objective function w.r.t. P_prev.
    """

    # all the params that are permuted by this permutation matrix, together with the axis on which it acts
    # e.g. ('layer_0.weight', 0), ('layer_0.bias', 0), ('layer_1.weight', 0)..
    params_and_axes: List[Tuple[str, int]] = perm_to_axes[P_curr_name]

    obj = 0.0

    for params_name, axis in params_and_axes:

        # axis != 0 will be considered when P_curr will be P_prev for some next layer
        if axis == 0:

            Wa, Wb = params_a[params_name], params_b[params_name]
            assert Wa.shape == Wa.shape

            P_prev_name, P_prev = get_prev_permutation(params_name, perm_to_axes, perm_matrices)

            layer_similarity = compute_layer_similarity(Wa, Wb, P_curr, P_prev, debug=debug)

            obj += layer_similarity

    return obj


def compute_layer_similarity(Wa, Wb, P_curr, P_prev, debug=True):
    # (P_i Wb_i) P_{i-1}^T

    # (P_i Wb_i)
    Wb_perm = perm_rows(perm=P_curr, x=Wb)
    if len(Wb.shape) == 2 and debug:
        assert torch.allclose(Wb_perm, P_curr @ Wb)

    if P_prev is not None:
        # (P_i Wb_i) P_{i-1}^T
        Wb_perm = perm_cols(x=Wb_perm, perm=P_prev.T)

        if len(Wb.shape) == 2 and debug:
            assert torch.allclose(Wb_perm, P_curr @ Wb @ P_prev.T)

    if len(Wa.shape) == 1:
        # vector case, result is the dot product of the vectors A^T B
        return Wa.T @ Wb_perm
    elif len(Wa.shape) == 2:
        # matrix case, result is the trace of the matrix product A^T B
        return torch.trace(Wa.T @ Wb_perm).numpy()
    elif len(Wa.shape) == 3:
        # tensor case, trace of a generalized inner product where the last dimensions are multiplied and summed
        return torch.trace(torch.einsum("ijk,jnk->in", Wa.transpose(1, 0), Wb_perm)).numpy()
    else:
        return torch.trace(torch.einsum("ijkm,jnkm->in", Wa.transpose(1, 0), Wb_perm)).numpy()


def compute_gradient_P_curr(Wa, Wb, P_prev):
    """
    (A P_{l-1} B^T)
    """

    if P_prev is None:
        P_prev = torch.eye(Wb.shape[1])

    assert Wa.shape == Wb.shape
    assert P_prev.shape[0] == Wb.shape[1]

    # P_{l-1} B^T
    Wb_perm = perm_rows(x=Wb.transpose(1, 0), perm=P_prev)
    if len(Wb.shape) == 2:
        assert torch.allclose(Wb_perm, P_prev @ Wb.T, atol=1e-6)

    if len(Wa.shape) == 2:
        grad_P_curr = Wa @ Wb_perm
    elif len(Wa.shape) == 3:
        grad_P_curr = torch.einsum("ijk,jnk->in", Wa, Wb_perm)
    else:
        grad_P_curr = torch.einsum("ijkm,jnkm->in", Wa, Wb_perm)

    return grad_P_curr


def compute_gradient_P_prev(Wa, Wb, P_curr):
    """
    (A^T P_l B)

    """
    assert P_curr.shape[0] == Wb.shape[0]

    grad_P_prev = None

    # (P_l B)
    Wb_perm = perm_rows(perm=P_curr, x=Wb)
    if len(Wb.shape) == 2:
        assert torch.all(Wb_perm == P_curr @ Wb)

    if len(Wa.shape) == 2:
        grad_P_prev = Wa.T @ Wb_perm
    elif len(Wa.shape) == 3:
        grad_P_prev = torch.einsum("ijk,jnk->in", Wa.transpose(1, 0), Wb_perm)
    else:
        grad_P_prev = torch.einsum("ijkm,jnkm->in", Wa.transpose(1, 0), Wb_perm)

    return grad_P_prev


def get_prev_permutation(params_name, perm_to_axes, perm_matrices):
    P_prev_name, P_prev = None, None

    for other_perm_name, other_perm in perm_matrices.items():

        # all the layers that are column-permuted by other_p
        params_perm_by_other_p = [tup[0] if tup[1] == 1 else None for tup in perm_to_axes[other_perm_name]]
        if params_name in params_perm_by_other_p:
            P_prev_name = other_perm_name
            P_prev = other_perm

    return P_prev_name, P_prev


def perm_rows(x, perm):
    """
    X ~ (n, d0) or (n, d0, d1) or (n, d0, d1, d2)
    perm ~ (n, n)
    """
    assert x.shape[0] == perm.shape[0]
    assert perm.dim() == 2 and perm.shape[0] == perm.shape[1]

    input_dims = "jklm"[: x.dim()]
    output_dims = "iklm"[: x.dim()]

    ein_string = f"ij,{input_dims}->{output_dims}"

    return torch.einsum(ein_string, perm, x)


def perm_cols(x, perm):
    """
    X ~ (d0, n) or (d0, d1, n) or (d0, d1, d2, n)
    perm ~ (n, n)
    """
    assert x.shape[1] == perm.shape[0]
    assert perm.shape[0] == perm.shape[1]

    x = x.transpose(1, 0)
    perm = perm.transpose(1, 0)

    permuted_x = perm_rows(x=x, perm=perm)

    return permuted_x.transpose(1, 0)


def update_perm_matrices(perm_matrices, proj_grads, step_size):
    new_perm_matrices = {}

    for perm_name, perm in perm_matrices.items():

        if perm_name in {"P_final", "P_4"}:
            new_perm_matrices[perm_name] = perm
            continue

        proj_grad = proj_grads[perm_name]

        new_P_curr_interp = (1 - step_size) * perm + step_size * proj_grad
        new_perm_matrices[perm_name] = new_P_curr_interp

    return new_perm_matrices


def sinkhorn_knopp(matrix, tol=1e-10, max_iterations=1000):
    """
    Applies the Sinkhorn-Knopp algorithm to make a non-negative matrix doubly stochastic.

    Parameters:
    matrix (2D torch tensor): A non-negative matrix.
    tol (float): Tolerance for the stopping condition.
    max_iterations (int): Maximum number of iterations.

    Returns:
    2D torch tensor: Doubly stochastic matrix.
    """
    if not torch.all(matrix >= 0):
        raise ValueError("Matrix contains negative values.")

    R, C = matrix.size()

    if R != C:
        raise ValueError("Matrix must be square.")

    # Normalize the matrix so that all elements sum to 1
    matrix = matrix / (matrix.sum() + 1e-16)

    # Initialize row and column scaling factors
    row_scale = torch.ones(R)
    col_scale = torch.ones(C)

    for _ in range(max_iterations):
        # Scale rows
        row_scale = 1 / (torch.mv(matrix, col_scale) + 1e-16)
        matrix = torch.diag(row_scale).mm(matrix)

        # Scale columns
        col_scale = 1 / (torch.mv(matrix.t(), row_scale) + 1e-16)
        matrix = matrix.mm(torch.diag(col_scale))

        # Check if matrix is close enough to doubly stochastic
        if torch.all(torch.abs(matrix.sum(dim=0) - 1) < tol) and torch.all(torch.abs(matrix.sum(dim=1) - 1) < tol):
            return matrix

    return matrix
