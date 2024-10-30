# Owner(s): ["module: inductor"]
import contextlib
import importlib
import unittest
from typing import Any, Callable, Optional, Tuple

import torch
import torch.utils._pytree as pytree
from torch._inductor import config
from torch._inductor.runtime.hints import TRITON_MAX_BLOCK
from torch._inductor.runtime.runtime_utils import is_power_of_2
from torch._inductor.test_case import TestCase as InductorTestCase
from torch._inductor.utils import run_and_get_code
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
)
from torch.testing._internal.inductor_utils import (
    GPU_TYPE,
    HAS_GPU,
    requires_gpu,
    skip_windows_ci,
)


skip_windows_ci(__name__, __file__)

importlib.import_module("filelock")

max_block: int = TRITON_MAX_BLOCK["X"]


@requires_gpu()
@config.patch("triton.use_block_ptr", True)
@instantiate_parametrized_tests
class TritonBlockPointerTest(InductorTestCase):
    def _discontiguous_tensor(
        self, view_size: Tuple[int, ...], device=torch.device(GPU_TYPE)
    ) -> torch.Tensor:
        """
        Create a padded tensor of the given size.
        The strides correspond to a tensor that is twice as large in each dimension.
        """
        full_size = tuple(2 * dim for dim in view_size)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())
        return view

    def run_and_compare(
        self,
        func: Callable[..., Any],
        *args,
        compile_kwargs: Optional[dict] = None,
        expected_num_block_pointers: Optional[int] = None,
        expected_num_programs: int = 1,
        expected_num_triton_kernels: int = 1,
        config_patches: Optional[dict] = None,
    ):
        """
        Runs the module through Inductor, comparing to eager reference.
        """
        if compile_kwargs is None:
            compile_kwargs = {}
        if config_patches is None:
            config_patches = {}

        def flatten_tensors(tensors):
            flat, spec = pytree.tree_flatten(tensors)
            return flat

        with config.patch(config_patches):
            compiled = torch.compile(func, backend="inductor", **compile_kwargs)
            result, code = run_and_get_code(compiled, *args)

        # Check numerical accuracy
        ref_tensors = flatten_tensors(func(*args))
        actual_tensors = flatten_tensors(result)
        for ref, actual in zip(ref_tensors, actual_tensors):
            self.assertTrue(torch.allclose(ref, actual))

        def count_code(substr: str, expected: Optional[int]):
            count = sum(prog.count(substr) for prog in code)
            if expected is not None:
                self.assertEqual(count, expected)

        # Check the code
        self.assertEqual(len(code), expected_num_programs)
        count_code("@triton.jit", expected_num_triton_kernels)
        count_code("tl.make_block_ptr", expected_num_block_pointers)

        return result, code

    @parametrize(
        "expected_num_block_pointers,raises",
        [
            (3, False),  # This should pass
            (9, True),  # This should fail
        ],
    )
    def test_expected_num_block_pointers(
        self, expected_num_block_pointers: int, raises: bool
    ):
        """
        Checks that the test harness verifies the number of block pointers correctly.
        """

        def foo(x, y):
            return x + y

        device = torch.device(GPU_TYPE)
        inputs = [torch.randn(8).to(device) for arg_idx in range(2)]

        # Expect failure for bad inputs
        with self.assertRaises(AssertionError) if raises else contextlib.nullcontext():
            # Expect 3 block pointers: 2 inputs 1 output
            self.run_and_compare(
                foo, *inputs, expected_num_block_pointers=expected_num_block_pointers
            )

    @parametrize("prefer_nd_tiling", [False, True])
    @parametrize(
        "full_size,view_size,stride,offset,require_block_ptr",
        [
            ((64, 32, 32), (32, 16, 8), None, None, True),
            ((16, 8, 8, 8), (8, 8, 4, 2), None, None, True),
            ((8, 8, 8, 8), (4, 4, 4, 4), None, None, True),
            ((8, 8), (4, 4), None, 10, True),  # Storage offset
            ((8, 8), (4, 4), (16, 2), None, True),  # Non-default strides
            ((8, 8), (4, 4), (1, 8), None, True),  # Transposed strides
            (
                (5, 9),
                (5, 8),
                None,
                None,
                True,
            ),  # Non-power-of-2 leading dim: block ptr
            (
                (15, 9),
                (15, 3),
                None,
                None,
                False,
            ),  # Non-power-of-2 inner dims: non-block ptr
            ((1, 1, 1), (1, 1, 1), None, None, False),  # Scalar: non-block ptr
            (
                (2, 4 * max_block),
                (2, 3 * max_block),
                None,
                None,
                True,
            ),  # Inner dim multiple of max_block
        ],
    )
    def test_pointwise(
        self,
        full_size: Tuple[int],
        view_size: Tuple[int],
        stride: Optional[Tuple[int]],
        offset: Optional[int],
        require_block_ptr: bool,
        prefer_nd_tiling: bool,
    ):
        """
        Test generating strided ND block pointers for a pointwise kernel.

        If require_block_ptr is True, the generated code must contain block
        pointers. However, ND block pointers are not supported for all shapes. So
        we also test some odd shapes with require_block_ptr set to False, to ensure that
        block pointer analysis does not break these cases.
        """

        def get_input() -> torch.Tensor:
            device = torch.device(GPU_TYPE)
            full = torch.randn(full_size).to(device)

            # Use the original tensor's stride by default
            view_stride = full.stride() if stride is None else stride

            return torch.as_strided(full, view_size, view_stride, storage_offset=offset)

        args = [get_input() for arg_idx in range(2)]

        # Expect 3 block pointers: 2 inputs 1 output
        self.run_and_compare(
            torch.add,
            *args,
            expected_num_block_pointers=3 if require_block_ptr else None,
            config_patches={"triton.prefer_nd_tiling": prefer_nd_tiling},
        )

    @parametrize("prefer_nd_tiling", [False, True])
    @parametrize(
        "x_size,y_size",
        [
            ((8, 8), (8, 1)),
            ((8, 8), (1, 8)),
            (
                (4, 1, 4),
                (1, 4, 1),
            ),  # Very important case: index variables are disjoint!
            (
                (1, 1, 1, 4),
                (4, 4, 4, 4),
            ),  # Unmatched dims for first operand.
        ],
    )
    def test_broadcast(
        self, x_size: Tuple[int], y_size: Tuple[int], prefer_nd_tiling: bool
    ):
        """
        Test that we can generate strided block pointers when inputs have different
        shapes, and they are broadcast together.
        """

        def foo(x, y):
            a = x + 1
            b = y * 2
            return a + b

        x, y = (self._discontiguous_tensor(size) for size in (x_size, y_size))

        # Check that input sizes are not the same
        self.assertNotEqual(x.shape, y.shape)

        # Check that at least one dimension is a singleton
        all_dims = x.shape + y.shape
        self.assertIn(1, all_dims)

        # Expect 3 block pointers: 2 inputs one output
        self.run_and_compare(
            foo,
            x,
            y,
            expected_num_block_pointers=3,
            config_patches={"triton.prefer_nd_tiling": prefer_nd_tiling},
        )

    @parametrize("prefer_nd_tiling", [False, True])
    def test_pointwise_broadcast_nonzero_strides(self, prefer_nd_tiling: bool):
        """
        Test that we emit tl.broadcast_to instead of using strides of 0.
        """

        full_shape = (8, 8)
        col_shape = (full_shape[1], 1)
        device = torch.device(GPU_TYPE)
        full = torch.randn(full_shape).to(device)
        col = torch.as_strided(full, col_shape, full.stride())

        # Expect 3 block pointers: 2 inputs one output
        result, (triton_code,) = self.run_and_compare(
            torch.add,
            full,
            col,
            expected_num_block_pointers=3,
            config_patches={
                "triton.prefer_nd_tiling": prefer_nd_tiling,
            },
        )

        # Check the code for broadcasts.
        # We shouldn't see any strides of 0.
        load_lines, store_lines = tuple(
            [line for line in triton_code.split("\n") if substr in line]
            for substr in ("tl.load", "tl.store")
        )
        if prefer_nd_tiling:
            self.assertExpectedInline(
                "\n".join(load_lines),
                """\
    tmp0 = tl.load(tl.make_block_ptr(in_ptr0, shape=[8, 8], strides=[1, 8], block_shape=[XBLOCK, YBLOCK], order=[1, 0], offsets=[xoffset, yoffset]), boundary_check=[0, 1])
    tmp1 = tl.load(tl.make_block_ptr(in_ptr1, shape=[8], strides=[8], block_shape=[YBLOCK], order=[0], offsets=[yoffset]), boundary_check=[0], eviction_policy='evict_last')[None, :]""",  # noqa: B950
            )
            self.assertExpectedInline(
                "\n".join(store_lines),
                """    tl.store(tl.make_block_ptr(out_ptr0, shape=[8, 8], strides=[1, 8], block_shape=[XBLOCK, YBLOCK], order=[1, 0], offsets=[xoffset, yoffset]), tmp2.to(tl.float32), boundary_check=[0, 1])""",  # noqa: B950
            )
        else:
            self.assertExpectedInline(
                "\n".join(load_lines),
                """\
    tmp0 = tl.load(tl.make_block_ptr(in_ptr0, shape=[64], strides=[1], block_shape=[XBLOCK], order=[0], offsets=[xoffset]), boundary_check=[0])
    tmp1 = tl.reshape(tl.broadcast_to(tl.load(tl.make_block_ptr(in_ptr1, shape=[8], strides=[8], block_shape=[((7 + XBLOCK) // 8)], order=[0], offsets=[(xoffset // 8)]), boundary_check=[0], eviction_policy='evict_last')[:, None, None], [((7 + XBLOCK) // 8), ((1) * ((1) <= (((7 + XBLOCK) // 8))) + (((7 + XBLOCK) // 8)) * ((((7 + XBLOCK) // 8)) < (1))), ((8) * ((8) <= (XBLOCK)) + (XBLOCK) * ((XBLOCK) < (8)))]), [XBLOCK])""",  # noqa: B950
            )
            self.assertExpectedInline(
                "\n".join(store_lines),
                """    tl.store(tl.make_block_ptr(out_ptr0, shape=[64], strides=[1], block_shape=[XBLOCK], order=[0], offsets=[xoffset]), tmp2.to(tl.float32), boundary_check=[0])""",  # noqa: B950
            )

    @parametrize("prefer_nd_tiling", [False, True])
    @parametrize(
        "view_size,num_block_pointers,num_triton_kernels",
        [
            ((4, 4), 1, 1),
            ((4, 4, 4), 1, 1),
            ((8, 8, 8), 1, 1),
            ((15, 15), None, 1),  # Non-power of 2
            ((3 * max_block, 2), 3, 2),  # Multiple of max block. Uses loops.
            (
                (2, 3 * max_block),
                2,
                2,
            ),  # Multiple of max block. Uses loops.
            ((128, 128), 3, 2),  # Test a large size, with loops.
        ],
    )
    def test_reduction(
        self,
        view_size: Tuple[int],
        num_block_pointers: int,
        num_triton_kernels: int,
        prefer_nd_tiling: bool,
    ):
        """
        Tests a reduction kernel.
        """

        view = self._discontiguous_tensor(view_size)

        # Expect at least 1 block pointer for the input.
        # Add 2 more if we generate 2 kernels.
        result, (code,) = self.run_and_compare(
            torch.sum,
            view,
            expected_num_block_pointers=num_block_pointers,
            expected_num_triton_kernels=num_triton_kernels,
            config_patches={"triton.prefer_nd_tiling": prefer_nd_tiling},
        )

    @parametrize(
        "view_size,num_block_pointers,num_triton_kernels",
        [
            ((8, 8), 2, 1),  # No loops. Should be supported.
            (
                (128, 128),
                None,
                None,
            ),  # Looped reduction. Block pointers not yet supported.
        ],
    )
    def test_mixed_pointwise_reduction(
        self, view_size: Tuple[int], num_block_pointers: int, num_triton_kernels: int
    ):
        """
        Tests mixing pointwise with reduction ops.
        """

        def foo(x, y):
            return torch.sum(x + y)

        inputs = [self._discontiguous_tensor(view_size) for input_idx in range(2)]

        # Expect 2 block pointers: inputs
        result, (code,) = self.run_and_compare(
            foo,
            *inputs,
            expected_num_block_pointers=num_block_pointers,
            expected_num_triton_kernels=num_triton_kernels,
        )

    def test_multiple_max_block_non_power_of_2(self):
        """
        Check that we support dims of size n * MAX_BLOCK, where n is any positive integer, not
        necessarily a power of 2.
        """

        def foo(x):
            return x - 1

        device = torch.device(GPU_TYPE)
        full_size = (3 * max_block, 3)
        view_size = (3 * max_block, 2)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())

        # Check that we're using dims that aren't all powers of 2
        have_np2_dim = not all(is_power_of_2(dim) for dim in view_size)
        self.assertTrue(have_np2_dim)

        # Check that we need more than one stride to represent the tensor
        nontrivial_dims = [dim for dim in view_size if dim > 1]
        self.assertTrue(len(nontrivial_dims) > 1)

        # Expect 2 block pointers: input and output
        self.run_and_compare(foo, view, expected_num_block_pointers=2)

    def test_dynamic_shapes_generic(self):
        """
        Test a generic strided block with dynamic shapes. Block pointers are not
        expected. This only checks that the analysis doesn't break this case.
        """

        device = torch.device(GPU_TYPE)
        full_size = (8, 8)
        view_size = (4, 4)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())

        self.run_and_compare(torch.div, view, view, compile_kwargs={"dynamic": True})

    @unittest.skip(reason="Dynamo tracing error")
    def test_dynamic_shapes_multiple_max_block(self):
        """
        Test dynamic shapes, where we know the shape is a multiple of the max block
        size. We should be able to generate a block pointer for this case.
        """

        def foo(x):
            tile_dims = (3 * max_block * x.shape[0], 3 * x.shape[1])
            view_size = (3 * max_block * x.shape[0], 2 * x.shape[1])
            full = x.tile(tile_dims)
            view = torch.as_strided(full, view_size, full.stride())
            return view + view

        device = torch.device(GPU_TYPE)
        x_size = (1, 1)
        x = torch.randn(x_size).to(device)

        # Expect 2 block pointers: input and output
        self.run_and_compare(
            x, compile_kwargs={"dynamic": True}, expected_num_block_pointers=2
        )

    @parametrize(
        "full_size,view_size,num_block_pointers,num_tiles",
        [
            (
                (32, 32),
                (16, 32),
                3,
                1,
            ),  # Contiguous 2D tensor. Does not require tiling.
            ((5, 9), (3, 7), 3, 2),  # 2D tensor with 1 discontiguous dim.
            ((11, 13, 7), (9, 13, 5), 3, 2),  # 3D tensor with 1 discontiguous dim (2).
            (
                (3, 11, 13, 7),
                (2, 9, 13, 7),
                3,
                2,
            ),  # 4D tensor with 1 discontiguous dim (1).
            (
                (3, 11, 13, 7),
                (2, 11, 9, 7),
                3,
                2,
            ),  # 4D tensor with 1 discontiguous dim (2).
            (
                (5, 5, 5, 5, 5),
                (3, 3, 5, 3, 5),
                1,
                2,
            ),  # 5D tensor with 2 discontiguous dims (3, 1). Block pointers unexpected.
        ],
    )
    def test_nd_tiling_odd_shapes_pointwise(
        self,
        full_size: Tuple[int],
        view_size: Tuple[int],
        num_block_pointers: int,
        num_tiles: int,
    ):
        """
        Test odd shapes with ND tiling enabled.
        Uses a pointwise op.
        """

        def get_input() -> torch.Tensor:
            device = torch.device(GPU_TYPE)
            full = torch.randn(full_size).to(device)
            return torch.as_strided(full, view_size, full.stride())

        args = [get_input() for arg_idx in range(2)]

        # Expect up to 3 block pointers: 2 inputs 1 output.
        result, code = self.run_and_compare(
            torch.add,
            *args,
            expected_num_block_pointers=num_block_pointers,
            config_patches={
                "triton.prefer_nd_tiling": True,
            },
        )

        # Check the code for the expected tiling.
        all_tiles = ("XBLOCK", "YBLOCK", "ZBLOCK")
        expected_tiles = set(all_tiles[:num_tiles])
        for tile_name in all_tiles:
            for program in code:
                if tile_name in expected_tiles:
                    self.assertIn(tile_name, program)
                else:
                    self.assertNotIn(tile_name, program)

    @parametrize(
        "view_size,num_block_pointers,num_triton_kernels,reduction_op",
        [
            ((15, 15), 1, 1, torch.sum),  # Non-power-of 2 shapes.
            ((129, 129), 3, 2, torch.sum),  # Large size, with loops.
            ((3, 3), 1, 1, torch.argmax),
            ((129, 129), 1, 1, torch.argmax),
        ],
    )
    def test_2d_reduction_odd_shapes(
        self,
        view_size: Tuple[int],
        num_block_pointers: int,
        num_triton_kernels: int,
        reduction_op: Callable,
    ):
        """
        Tests 2D reduction kernels. These arise from "odd" shapes which are not
        expressible with a 1D block pointer.
        """
        view = self._discontiguous_tensor(view_size)

        # Expect at least 1 block pointer for the input.
        # Add 2 more if we generate 2 kernels.
        result, (code,) = self.run_and_compare(
            reduction_op,
            view,
            expected_num_block_pointers=num_block_pointers,
            expected_num_triton_kernels=num_triton_kernels,
            config_patches={"triton.prefer_nd_tiling": True},
        )

        # Check the code for multiple Rn_BLOCK's
        for block in ["R0_BLOCK", "R1_BLOCK"]:
            self.assertIn(block, code)

    def test_2d_reduction_no_x_dim(self):
        """
        Tests a 2D reduction without an "x" dimension.
        """
        # We need a size to get no x dim.
        view = self._discontiguous_tensor((2, 346))

        # Expect 1 block pointer for the input.
        result, (code,) = self.run_and_compare(
            torch.prod,
            view,
            expected_num_block_pointers=1,
            expected_num_triton_kernels=1,
            config_patches={"triton.prefer_nd_tiling": True},
        )

        # Check that there's no X dimension in the signature.
        (signature_line,) = (
            line for line in code.splitlines() if line.startswith("def triton")
        )
        self.assertNotIn("BLOCK", signature_line)

        # Check for 2 reduction dimensions in the body.
        for block in ["R0_BLOCK", "R1_BLOCK"]:
            self.assertIn(block, code)

    @parametrize(
        "size,expected_num_block_pointers,expected_num_triton_kernels,expect_fallback",
        [
            ((8, 8), 1, 2, True),  # Persistent Welford fallback
            ((128, 128), 9, 3, False),  # Looped Welford reduction
        ],
    )
    def test_2d_welford_reduction(
        self,
        size: Tuple[int],
        expected_num_block_pointers: int,
        expected_num_triton_kernels: int,
        expect_fallback: bool,
    ):
        """
        Tests a 2D welford reduction.

        NB: the input size should be "nice" in the sense that it's a multiple of the
        number of processors. Otherwise, we will get more complex indexing that
        doesn't generate a block pointer. Since tiling welford reductions depends on
        the block pointer analysis, those cases would fall back to 1D.
        """
        view = self._discontiguous_tensor(size)

        # We expect many block pointers for this one.
        result, (code,) = self.run_and_compare(
            torch.var_mean,
            view,
            expected_num_block_pointers=expected_num_block_pointers,
            expected_num_triton_kernels=expected_num_triton_kernels,
            config_patches={"triton.prefer_nd_tiling": True},
        )

        # Check for a Welford reduction.
        self.assertEqual("welford" in code, not expect_fallback)

        # Check for 2 reduction dimensions.
        for block in ["R0_BLOCK", "R1_BLOCK"]:
            self.assertIn(block, code)

    def test_welford_non_block_pointer(
        self,
    ):
        """
        Tests a welford reduction where block pointer analysis fails.
        The main loop will be a 1D reduction, instead of 2D.
        """
        # Use a "bad" size that's not evenly divisible by the launch grid.
        # This won't decompose into a block pointer.
        view = self._discontiguous_tensor((259, 311))

        # We expect many block pointers for this one.
        result, (code,) = self.run_and_compare(
            torch.var_mean,
            view,
            expected_num_block_pointers=6,
            expected_num_triton_kernels=3,
            config_patches={"triton.prefer_nd_tiling": True},
        )

        # Check for a Welford reduction.
        self.assertIn("welford", code)

        # Check for a single reduction dimension.
        self.assertIn("R0_BLOCK", code)
        self.assertNotIn("R1_BLOCK", code)

    def test_reduction_multiple_discontiguous_dims(self):
        """
        Test reducing a tensor with more than one discontiguous dimension. This case
        won't generate a block pointer, since we don'allow enough tiling dimensions.
        """
        # Use odd shapes to frustrate block pointer analysis.
        view = self._discontiguous_tensor((3, 7, 11))

        result, (code,) = self.run_and_compare(
            torch.sum,
            view,
            expected_num_block_pointers=0,
            expected_num_triton_kernels=1,
            config_patches={"triton.prefer_nd_tiling": True},
        )

        # Check for 2 reduction dimensions.
        for block in ["R0_BLOCK", "R1_BLOCK"]:
            self.assertIn(block, code)

    def test_complex_reshape_block_ptr(self):
        def func(x, y):
            add_ = x + y
            reshape_0 = add_.reshape([8, 16, 128])
            permute_0 = reshape_0.permute([0, 2, 1])
            reshape_1 = permute_0.reshape([1024, 16])
            clone_0 = reshape_1.clone(memory_format=torch.contiguous_format)
            permute_1 = clone_0.permute([1, 0])
            clone_1 = permute_1.clone(memory_format=torch.contiguous_format)

            return clone_0, clone_1

        inps = (torch.rand((8, 2048), device=GPU_TYPE, dtype=torch.float32),) * 2
        result, code = self.run_and_compare(
            func,
            *inps,
            expected_num_triton_kernels=2,
            expected_num_block_pointers=4,
        )
        self.assertTrue("Min" not in code[0])


if __name__ == "__main__":
    from torch._inductor.test_case import run_tests

    if HAS_GPU:
        run_tests(needs="filelock")
