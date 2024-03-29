################################################################################
# © Copyright 2021-2022 Zapata Computing Inc.
################################################################################
from functools import wraps
from typing import Callable, List, Tuple

import numpy as np
import pytest
from orquestra.opt.api.cost_function import CostFunction
from orquestra.opt.api.optimizer import optimization_result
from orquestra.opt.api.optimizer_test import NESTED_OPTIMIZER_CONTRACTS
from orquestra.opt.mock_objects import MockOptimizer
from orquestra.quantum.api.estimation import EstimationTask
from orquestra.quantum.estimation import estimate_expectation_values_by_averaging
from orquestra.quantum.operators import PauliRepresentation, PauliTerm
from orquestra.quantum.runners import SymbolicSimulator

from orquestra.vqa.ansatz import QAOAFarhiAnsatz
from orquestra.vqa.api.ansatz import Ansatz
from orquestra.vqa.cost_function import (
    create_cost_function,
    substitution_based_estimation_tasks_factory,
)
from orquestra.vqa.opt.recursive_qaoa import (
    RecursiveQAOA,
    _create_default_qubit_map,
    _create_reduced_hamiltonian,
    _find_term_with_strongest_correlation,
    _map_reduced_solutions_to_original_solutions,
    _update_qubit_map,
)
from orquestra.vqa.shot_allocation._shot_allocation import allocate_shots_uniformly


class TestRQAOA:
    @pytest.fixture()
    def hamiltonian(self):
        return (
            PauliTerm("Z0*Z1", 5)
            + PauliTerm("Z0*Z3", 2)
            + PauliTerm("Z1*Z2", 0.5)
            + PauliTerm("Z2*Z3", 0.6)
        )

    @pytest.fixture()
    def ansatz(self, hamiltonian):
        return QAOAFarhiAnsatz(1, hamiltonian)

    @pytest.fixture()
    def cost_function_factory(
        self,
    ) -> Callable[[PauliRepresentation, Ansatz], CostFunction]:
        def _cf_factory(
            target_operator: PauliRepresentation,
            ansatz: Ansatz,
        ):
            # NOTE: Partial would be easier to use, but mypy doesn't work well
            # with partials, so I decided to go with a closure, but leaving
            # old code as reference.

            # estimation_preprocessors = [
            #     partial(allocate_shots_uniformly, number_of_shots=1000)
            # ]
            def allocate_shots_uniformly_preprocessor(
                estimation_tasks: List[EstimationTask],
            ) -> List[EstimationTask]:
                return allocate_shots_uniformly(
                    estimation_tasks=estimation_tasks, number_of_shots=1000
                )

            estimation_preprocessors = [allocate_shots_uniformly_preprocessor]
            estimation_tasks_factory = substitution_based_estimation_tasks_factory(
                target_operator,
                ansatz,
                estimation_preprocessors=estimation_preprocessors,
            )
            return create_cost_function(
                runner=SymbolicSimulator(),
                estimation_tasks_factory=estimation_tasks_factory,
                estimation_method=estimate_expectation_values_by_averaging,
                parameter_preprocessors=None,
            )

        return _cf_factory

    @pytest.fixture()
    def opt_params(self):
        # Pre-figured-out optimal params for the hamiltonian fixture
        return np.array([-0.34897497, 4.17835486])

    @pytest.fixture()
    def inner_optimizer(self, opt_params):
        inner_optimizer = MockOptimizer()

        def custom_minimize(
            cost_function: CostFunction,
            initial_params: np.ndarray,
            keep_history: bool = False,
        ):
            return optimization_result(
                opt_value=cost_function(opt_params),  # type: ignore
                opt_params=opt_params,
                nfev=1,
                nit=1,
                history=[],
            )

        inner_optimizer._minimize = custom_minimize
        return inner_optimizer

    @pytest.mark.parametrize("contract", NESTED_OPTIMIZER_CONTRACTS)
    def test_if_satisfies_contracts(
        self, contract, ansatz, cost_function_factory, inner_optimizer, hamiltonian
    ):
        initial_params = np.array([0.42, 4.2])
        recursive_qaoa = RecursiveQAOA(
            ansatz=ansatz,
            cost_hamiltonian=hamiltonian,
            inner_optimizer=inner_optimizer,
            n_c=2,
        )

        assert contract(recursive_qaoa, cost_function_factory, initial_params)

    @pytest.mark.parametrize("n_c", [-1, 0, 4, 5])
    def test_RQAOA_raises_exception_if_n_c_is_incorrect_value(
        self,
        hamiltonian,
        ansatz,
        inner_optimizer,
        n_c,
    ):
        with pytest.raises(ValueError):
            RecursiveQAOA(
                n_c,
                hamiltonian,
                ansatz,
                inner_optimizer,
            )

    @pytest.mark.parametrize(
        "n_qubits, expected_qubit_map",
        [
            (2, {0: [0, 1], 1: [1, 1]}),
            (
                7,
                {
                    0: [0, 1],
                    1: [1, 1],
                    2: [2, 1],
                    3: [3, 1],
                    4: [4, 1],
                    5: [5, 1],
                    6: [6, 1],
                },
            ),
        ],
    )
    def test_create_default_qubit_map(self, n_qubits, expected_qubit_map):
        qubit_map = _create_default_qubit_map(n_qubits)
        assert qubit_map == expected_qubit_map

    def test_find_term_with_strongest_correlation(
        self,
        hamiltonian,
        ansatz,
        opt_params,
        cost_function_factory,
    ):

        (
            term_with_largest_expval,
            largest_expval,
        ) = _find_term_with_strongest_correlation(
            hamiltonian,
            ansatz,
            opt_params,
            cost_function_factory,
        )
        assert term_with_largest_expval == PauliTerm("Z0*Z1", 5)
        assert np.sign(largest_expval) == -1

    @pytest.mark.parametrize(
        "term_with_largest_expval, largest_expval, expected_reduced_ham",
        [
            (
                # suppose we want to get rid of qubit 1 and replace it with qubit 0.
                PauliTerm("Z0*Z1", 5.0),
                10,
                (
                    PauliTerm("Z0*Z2", 2.0)
                    + PauliTerm("Z0*Z1", 0.5)
                    + PauliTerm("Z1*Z2", 0.6)
                ),
            ),
            (
                PauliTerm("Z0*Z1", 5.0),
                -10,
                (
                    PauliTerm("Z0*Z2", 2.0)
                    + PauliTerm("Z0*Z1", -0.5)
                    + PauliTerm("Z1*Z2", 0.6)
                ),
            ),
            (
                PauliTerm("Z0*Z3", 2),
                -10,
                (
                    PauliTerm("Z0*Z1", 5)
                    + PauliTerm("Z1*Z2", 0.5)
                    + PauliTerm("Z2*Z0", -0.6)
                ),
            ),
        ],
    )
    def test_reduce_cost_hamiltonian(
        self,
        hamiltonian,
        term_with_largest_expval,
        largest_expval,
        expected_reduced_ham,
    ):
        reduced_ham = _create_reduced_hamiltonian(
            hamiltonian, term_with_largest_expval, largest_expval
        )
        assert reduced_ham.terms == expected_reduced_ham.terms

    @pytest.mark.parametrize(
        "term_with_largest_expval, largest_expval, expected_new_qubit_map",
        [
            (
                # suppose we want to get rid of qubit 1 and replace it with qubit 0.
                PauliTerm("Z0*Z1", 5.0),
                10,
                {0: [0, 1], 1: [0, 1], 2: [1, 1], 3: [2, 1]},
            ),
            (
                PauliTerm("Z0*Z1", 5.0),
                -10,
                {0: [0, 1], 1: [0, -1], 2: [1, 1], 3: [2, 1]},
            ),
            (
                PauliTerm("Z0*Z3", 2),
                -10,
                {0: [0, 1], 1: [1, 1], 2: [2, 1], 3: [0, -1]},
            ),
        ],
    )
    def test_update_qubit_map(
        self,
        term_with_largest_expval,
        largest_expval,
        expected_new_qubit_map,
    ):
        qubit_map = _create_default_qubit_map(4)

        new_qubit_map = _update_qubit_map(
            qubit_map, term_with_largest_expval, largest_expval
        )
        assert new_qubit_map == expected_new_qubit_map

    def test_update_qubit_map_works_properly_on_subsequent_recursions(self):
        # (This test is for when the qubit map to be updated is not the default one)
        qubit_map = {0: [0, 1], 1: [1, 1], 2: [1, -1], 3: [2, 1], 4: [1, 1]}
        term_with_largest_expval = PauliTerm("Z0*Z1")
        largest_expval = -42

        # How the expected_new_qubit_map is calculated:
        # {0: [0, 1], 1: [1, 1], 2: [1, -1], 3: [2, 1], 4: [1, 1]} -> original
        #     qubit map
        # {0: [0, 1], 1: [1, -1], 2: [1, -1], 3: [2, 1], 4: [1, 1]} -> replace 1 with
        #     negative of 0
        # {0: [0, 1], 1: [0, -1], 2: [0, 1], 3: [2, 1], 4: [0, -1]} ->
        #     replace things that depends on 1 with negative of 0
        # {0: [0, 1], 1: [0, -1], 2: [0, 1], 3: [1, 1], 4: [0, -1]} -> nudge higher
        #     qubits down
        expected_new_qubit_map = {
            0: [0, 1],
            1: [0, -1],
            2: [0, 1],
            3: [1, 1],
            4: [0, -1],
        }
        new_qubit_map = _update_qubit_map(
            qubit_map, term_with_largest_expval, largest_expval
        )
        assert new_qubit_map == expected_new_qubit_map

    @pytest.mark.parametrize(
        "qubit_map, expected_original_solutions",
        [
            # Identity test w default qubit map
            ({0: [0, 1], 1: [1, 1]}, [(0, 1), (1, 0)]),
            ({0: [1, 1], 1: [1, 1], 2: [0, -1]}, [(1, 1, 1), (0, 0, 0)]),
            (
                {0: [0, 1], 1: [0, -1], 2: [0, 1], 3: [1, 1], 4: [0, -1]},
                [(0, 1, 0, 1, 1), (1, 0, 1, 0, 0)],
            ),
        ],
    )
    def test_map_reduced_solutions_to_original_solutions(
        self, qubit_map, expected_original_solutions
    ):
        reduced_solutions = [(0, 1), (1, 0)]
        original_solutions = _map_reduced_solutions_to_original_solutions(
            reduced_solutions, qubit_map
        )

        # Check lists are equal regardless of order
        assert set(expected_original_solutions) == set(original_solutions)

    def test_RQAOA_returns_correct_answer(
        self,
        hamiltonian,
        inner_optimizer,
        ansatz,
        cost_function_factory,
    ):
        initial_params = np.array([0.42, 4.2])

        recursive_qaoa = RecursiveQAOA(
            3,
            hamiltonian,
            ansatz,
            inner_optimizer,
        )

        opt_result = recursive_qaoa.minimize(cost_function_factory, initial_params)
        solutions = opt_result.opt_solutions

        n_qubits = 4
        for solution in solutions:
            assert len(solution) == n_qubits

        assert set(solutions) == set([(1, 0, 1, 0), (0, 1, 0, 1)])

    @pytest.mark.parametrize("n_c, expected_n_recursions", [(3, 1), (2, 2), (1, 3)])
    def test_RQAOA_performs_correct_number_of_recursions(
        self,
        hamiltonian,
        ansatz,
        inner_optimizer,
        cost_function_factory,
        n_c,
        expected_n_recursions,
    ):

        initial_params = np.array([0.42, 4.2])

        recursive_qaoa = RecursiveQAOA(
            n_c,
            hamiltonian,
            ansatz,
            inner_optimizer,
        )

        def counted_calls(f):
            """A wrapper for counting number of function calls.

            Borrowed from from stackoverflow.
            """

            @wraps(f)
            def count_wrapper(*args, **kwargs):
                count_wrapper.count += 1
                return f(*args, **kwargs)

            count_wrapper.count = 0
            return count_wrapper

        wrapped = counted_calls(recursive_qaoa._recursive_minimize)
        recursive_qaoa._recursive_minimize = wrapped
        opt_result = recursive_qaoa.minimize(cost_function_factory, initial_params)
        assert wrapped.count == expected_n_recursions

        solutions = opt_result.opt_solutions
        n_qubits = 4
        for solution in solutions:
            assert len(solution) == n_qubits

    @pytest.mark.parametrize("n_c, expected_n_recursions", [(3, 1), (2, 2), (1, 3)])
    def test_keeps_history_across_multiple_recursions(
        self,
        hamiltonian,
        ansatz,
        inner_optimizer,
        cost_function_factory,
        n_c,
        expected_n_recursions,
    ):

        initial_params = np.array([0.42, 4.2])

        recursive_qaoa = RecursiveQAOA(
            n_c,
            hamiltonian,
            ansatz,
            inner_optimizer,
        )

        opt_result = recursive_qaoa.minimize(
            cost_function_factory, initial_params, keep_history=True
        )

        # We know that our inner_optimizer does 1 iteration and 1 call to cost
        # function per recursion, therefore, opt_result.nfev and opt_result.nit and
        # length of opt_result.history should be equal to expected_n_recursions

        assert opt_result.nit == expected_n_recursions
        assert opt_result.nfev == expected_n_recursions
        assert len(opt_result.history) == expected_n_recursions
