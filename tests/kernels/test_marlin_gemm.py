"""Tests for the marlin kernel.

Run `pytest tests/kernels/marlin/test_marlin_gemm.py`.
"""
import pytest
import torch

from vllm import _custom_ops as ops
from vllm.model_executor.layers.quantization.gptq_marlin import (
    GPTQ_MARLIN_SUPPORTED_GROUP_SIZES, GPTQ_MARLIN_SUPPORTED_NUM_BITS)
from vllm.model_executor.layers.quantization.utils.marlin_utils import (
    MarlinWorkspace, is_marlin_supported, marlin_quantize, marlin_weights)
from vllm.model_executor.layers.quantization.utils.quant_utils import (
    gptq_pack, quantize_weights, sort_weights)

ACT_ORDER_OPTS = [False, True]
K_FULL_OPTS = [False, True]

K_CHUNKS = [128, 256]
N_CHUNKS = [64, 128, 256]

MNK_FACTORS = [
    (1, 1, 1),
    (1, 4, 8),
    (1, 7, 5),
    (1, 7 * 4, 5 * 1),
    (13, 17, 67),
    (26, 37, 13),
    (67, 13, 11),
]


def rand_data(shape):
    data = torch.rand(shape).to(torch.half).cuda()
    return data


@pytest.mark.skipif(not is_marlin_supported(),
                    reason="Marlin is not supported on this GPU type.")
@pytest.mark.parametrize("k_chunk", K_CHUNKS)
@pytest.mark.parametrize("n_chunk", N_CHUNKS)
@pytest.mark.parametrize("num_bits", GPTQ_MARLIN_SUPPORTED_NUM_BITS)
@pytest.mark.parametrize("group_size", GPTQ_MARLIN_SUPPORTED_GROUP_SIZES)
@pytest.mark.parametrize("act_order", ACT_ORDER_OPTS)
@pytest.mark.parametrize("mnk_factors", MNK_FACTORS)
def test_marlin_repack(k_chunk, n_chunk, num_bits, group_size, act_order,
                       mnk_factors):
    m_factor, n_factor, k_factor = mnk_factors

    size_m = m_factor
    size_k = k_chunk * k_factor
    size_n = n_chunk * n_factor

    print(f"MNK = {size_m} {size_n} {size_k}")

    # Filter act_order
    if act_order:
        if group_size == -1:
            return
        if group_size == size_k:
            return

    # Normalize group_size
    if group_size == -1:
        group_size = size_k
    assert group_size <= size_k

    # Create input
    b_weight = rand_data((size_k, size_n))

    # Quantize (and apply act_order if provided)
    w_ref, q_w, s, g_idx, rand_perm = quantize_weights(b_weight, num_bits,
                                                       group_size, act_order)

    # Pack to GPTQ format
    q_w_gptq = gptq_pack(q_w, num_bits, size_k, size_n)

    # For act_order, sort the "weights" and "g_idx" so that group ids are
    # increasing
    sort_indices = torch.empty(0, dtype=torch.int, device=b_weight.device)
    if act_order:
        q_w, g_idx, sort_indices = sort_weights(q_w, g_idx)

    # Pack to Marlin format
    marlin_q_w_1 = marlin_weights(q_w, size_k, size_n, num_bits)

    # Run Marlin repack GPU kernel
    marlin_q_w_2 = ops.gptq_marlin_repack(
        q_w_gptq,
        sort_indices,
        size_k,
        size_n,
        num_bits,
    )
    torch.cuda.synchronize()

    assert torch.allclose(marlin_q_w_1, marlin_q_w_2)


@pytest.mark.skipif(not is_marlin_supported(),
                    reason="Marlin is not supported on this GPU type.")
@pytest.mark.parametrize("k_chunk", K_CHUNKS)
@pytest.mark.parametrize("n_chunk", N_CHUNKS)
@pytest.mark.parametrize("num_bits", GPTQ_MARLIN_SUPPORTED_NUM_BITS)
@pytest.mark.parametrize("group_size", GPTQ_MARLIN_SUPPORTED_GROUP_SIZES)
@pytest.mark.parametrize("mnk_factors", MNK_FACTORS)
@pytest.mark.parametrize("act_order", ACT_ORDER_OPTS)
@pytest.mark.parametrize("is_k_full", K_FULL_OPTS)
def test_marlin_gemm(
    k_chunk,
    n_chunk,
    num_bits,
    group_size,
    mnk_factors,
    act_order,
    is_k_full,
):
    m_factor, n_factor, k_factor = mnk_factors

    size_m = m_factor
    size_k = k_chunk * k_factor
    size_n = n_chunk * n_factor

    print(f"MNK = {size_m} {size_n} {size_k}")
    print(f"groupsize = {group_size}")

    if act_order:
        if group_size == -1:
            return
        if group_size == size_k:
            return

    a_input = rand_data((size_m, size_k))
    b_weight = rand_data((size_k, size_n))

    w_ref, marlin_q_w, marlin_s, g_idx, sort_indices, _ = marlin_quantize(
        b_weight, num_bits, group_size, act_order)

    workspace = MarlinWorkspace(size_n)

    output = ops.gptq_marlin_gemm(
        a_input,
        marlin_q_w,
        marlin_s,
        g_idx,
        sort_indices,
        workspace.scratch,
        num_bits,
        a_input.shape[0],
        b_weight.shape[1],
        a_input.shape[1],
        is_k_full,
    )
    output_ref = torch.matmul(a_input, w_ref)

    torch.cuda.synchronize()

    assert torch.allclose(output, output_ref, rtol=1e-2)
