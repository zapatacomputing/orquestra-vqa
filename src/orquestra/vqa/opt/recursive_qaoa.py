################################################################################
# © Copyright 2021-2022 Zapata Computing Inc.
################################################################################
from collections import defaultdict
from copy import copy, deepcopy
from typing import Callable, Dict, List, Tuple

import numpy as np
from orquestra.opt.api.cost_function import CostFunction
from orquestra.opt.api.optimizer import (
    NestedOptimizer,
    Optimizer,
    extend_histories,
    optimization_result,
)
from orquestra.opt.history.recorder import HistoryEntry, RecorderFactory
from orquestra.opt.history.recorder import recorder as _recorder
from orquestra.opt.problems import solve_problem_by_exhaustive_search
from orquestra.quantum.operators import PauliRepresentation, PauliSum, PauliTerm
from scipy.optimize import OptimizeResult

from ..api.ansatz import Ansatz


class RecursiveQAOA(NestedOptimizer):
    @property
    def inner_optimizer(self) -> Optimizer:
        return self._inner_optimizer

    @property
    def recorder(self) -> RecorderFactory:
        return self._recorder

    def __init__(
        self,
        n_c: int,
        cost_hamiltonian: PauliRepresentation,
        ansatz: Ansatz,
        inner_optimizer: Optimizer,
        recorder: RecorderFactory = _recorder,
    ) -> None:
        """Recursive QAOA (RQAOA) optimizer

        Original paper: https://arxiv.org/abs/1910.08980 page 4.

        The main idea is that we call QAOA recursively and reduce the size of the cost
        hamiltonian by 1 on each recursion, until we hit a threshold number of qubits
        `n_c`. Then, we use brute force to solve the reduced QAOA problem, mapping the
        reduced solution to the original solution.

        Args:
            n_c: The threshold number of qubits at which recursion stops, as described
                in the original paper. Cannot be greater than number of qubits.
            cost_hamiltonian: Hamiltonian representing the cost function.
            ansatz: an Ansatz object with all params (ex. `n_layers`) initialized
            inner_optimizer: optimizer used for optimization of parameters at each
                recursion of RQAOA.
            recorder: recorder object defining how to store the optimization history.
        """
        n_qubits = cost_hamiltonian.n_qubits

        if n_c >= n_qubits or n_c <= 0:
            raise ValueError(
                "n_c needs to be a value less than number of qubits and greater than 0."
            )

        self._n_c = n_c
        self._ansatz = ansatz
        self._cost_hamiltonian = cost_hamiltonian
        self._inner_optimizer = inner_optimizer
        self._recorder = recorder

    def _minimize(
        self,
        cost_function_factory: Callable[[PauliRepresentation, Ansatz], CostFunction],
        initial_params: np.ndarray,
        keep_history: bool = False,
    ) -> OptimizeResult:
        """Args:
            cost_function_factory: function that generates CostFunction objects given
                the provided ansatz and cost_hamiltonian.
            initial_params: initial parameters used for optimization
            keep_history: flag indicating whether history of cost function evaluations
                should be recorded.

        Returns:
            OptimizeResult with the added entry of:
                opt_solutions (List[Tuple[int, ...]]): The solution(s) to recursive
                    QAOA as a list of tuples; each tuple is a tuple of bits.
        """

        n_qubits = self._cost_hamiltonian.n_qubits
        qubit_map = _create_default_qubit_map(n_qubits)

        histories: Dict[str, List[HistoryEntry]] = defaultdict(list)
        histories["history"] = []

        return self._recursive_minimize(
            cost_function_factory,
            initial_params,
            keep_history,
            cost_hamiltonian=self._cost_hamiltonian,
            qubit_map=qubit_map,
            nit=0,
            nfev=0,
            histories=histories,
        )

    def _recursive_minimize(
        self,
        cost_function_factory,
        initial_params,
        keep_history,
        cost_hamiltonian,
        qubit_map,
        nit,
        nfev,
        histories,
    ):
        """A method that recursively calls itself with each recursion reducing 1 term
        of the cost hamiltonian
        """

        # Set up QAOA circuit
        ansatz = copy(self._ansatz)

        ansatz.cost_hamiltonian = cost_hamiltonian

        cost_function = cost_function_factory(cost_hamiltonian, ansatz)

        if keep_history:
            cost_function = self.recorder(cost_function)

        # Run & optimize QAOA
        opt_results = self.inner_optimizer.minimize(cost_function, initial_params)
        nit += opt_results.nit
        nfev += opt_results.nfev
        if keep_history:
            histories = extend_histories(cost_function, histories)

        # Reduce the cost hamiltonian
        (
            term_with_largest_expval,
            largest_expval,
        ) = _find_term_with_strongest_correlation(
            cost_hamiltonian,
            ansatz,
            opt_results.opt_params,
            cost_function_factory,
        )

        new_qubit_map = _update_qubit_map(
            qubit_map, term_with_largest_expval, largest_expval
        )

        reduced_cost_hamiltonian = _create_reduced_hamiltonian(
            cost_hamiltonian,
            term_with_largest_expval,
            largest_expval,
        )

        # Check new cost hamiltonian has correct amount of qubits
        assert (
            reduced_cost_hamiltonian.n_qubits == cost_hamiltonian.n_qubits - 1
            # If we have 1 qubit, the reduced cost hamiltonian would be empty and say
            # it has 0 qubits.
            or reduced_cost_hamiltonian.n_qubits == 0
            and cost_hamiltonian.n_qubits == 2
            and self._n_c == 1
        )

        # Check qubit map has correct amount of qubits
        assert (
            cost_hamiltonian.n_qubits - 1
            == max([qubit_indices[0] for qubit_indices in new_qubit_map.values()]) + 1
        )

        if reduced_cost_hamiltonian.n_qubits > self._n_c:
            # If we didn't reach threshold `n_c`, we repeat the the above with the
            # reduced cost hamiltonian.
            return self._recursive_minimize(
                cost_function_factory,
                initial_params,
                keep_history,
                cost_hamiltonian=reduced_cost_hamiltonian,
                qubit_map=new_qubit_map,
                nit=nit,
                nfev=nfev,
                histories=histories,
            )

        else:
            best_value, reduced_solutions = solve_problem_by_exhaustive_search(
                reduced_cost_hamiltonian
            )

            solutions = _map_reduced_solutions_to_original_solutions(
                reduced_solutions, new_qubit_map
            )

            opt_result = optimization_result(
                opt_solutions=solutions,
                opt_value=best_value,
                opt_params=None,
                nit=nit,
                nfev=nfev,
                **histories,
            )

            return opt_result


def _create_default_qubit_map(n_qubits: int) -> Dict[int, List[int]]:
    """Creates a qubit map that maps each qubit to itself."""
    qubit_map = {}
    for i in range(n_qubits):
        qubit_map[i] = [i, 1]
    return qubit_map


def _find_term_with_strongest_correlation(
    hamiltonian: PauliRepresentation,
    ansatz: Ansatz,
    optimal_params: np.ndarray,
    cost_function_factory: Callable[[PauliRepresentation, Ansatz], CostFunction],
) -> Tuple[PauliTerm, float]:
    """Find term Z_i Z_j maximizing <psi(beta, gamma) | Z_i Z_j | psi(beta, gamma)>.

    The idea is that the term with largest expectation value also has the largest
    correlation or anticorrelation between its qubits, and this information can be used
    to eliminate a qubit. See equation (15) of the original paper.

    Args:
        hamiltonian: the hamiltonian that you want to find term with strongest
            correlation of.
        ansatz: ansatz representing the circuit of the full hamiltonian, used to
            calculate psi(beta, gamma)
        optimal_params: optimal values of beta, gamma
        cost_function_factory: See docstring of RecursiveQAOA

    Returns:
        The term with the largest correlation, and its expectation value.
    """
    largest_expval = 0.0

    term_with_largest_expval = hamiltonian.terms[0]

    for term in hamiltonian.terms:
        # If term is a constant term, don't calculate expectation value.
        if not term.is_constant:

            # Calculate expectation value of term
            cost_function_of_term = cost_function_factory(term, ansatz)
            expval_of_term = cost_function_of_term(optimal_params)  # type: ignore

            if np.abs(expval_of_term) > np.abs(largest_expval):
                largest_expval = expval_of_term
                term_with_largest_expval = term

    return (term_with_largest_expval, largest_expval)


def _update_qubit_map(
    qubit_map: Dict[int, List[int]],
    term_with_largest_expval: PauliTerm,
    largest_expval: float,
) -> Dict[int, List[int]]:
    """Update the qubit map according to equation (15) in the original paper.

    The process comprises the following steps:
        1. Substituting one qubit of `term_with_largest_expval` with the other
        2. Substituting all qubits larger than the gotten-rid-of-qubit with the qubit
           one below it

    Args:
        qubit_map: the qubit map to be updated.
        term_with_largest_expval: term with largest expectation value
        largest_expval: the expectation value of `term_with_largest_expval`

    Note:
        Qubit map is a dictionary that maps qubits in reduced Hamiltonian back to
        original qubits.

        Example:
            `qubit_map = {0: [2, -1], 1: [3, 1]]}
                Keys are the original qubit indices.
                1st term of inner list is qubit the index of tuple to be mapped onto,
                2nd term is if it will be mapped onto the same value or opposite of
                    the qubit it is being mapped onto.
                In the above qubit_map, the original qubit 0 is now represented by the
                    opposite value of qubit 2, and the original qubit 1 is now
                    represented by the value of qubit 3.
    """
    new_qubit_map = deepcopy(qubit_map)

    # Get rid of the larger qubit in the term.
    qubit_to_get_rid_of = sorted(term_with_largest_expval.qubits)[-1]

    # i is original qubit, qubit_map[i][0] is current equivalent of original qubit.
    for i in range(len(new_qubit_map)):
        if new_qubit_map[i][0] == qubit_to_get_rid_of:
            new_qubit_map[i][1] *= int(np.sign(largest_expval))
        new_qubit_map[i][0] = _get_new_qubit_indice(
            new_qubit_map[i][0], term_with_largest_expval
        )

    return new_qubit_map


def _get_new_qubit_indice(
    old_indice: int, operator_with_largest_expval: PauliTerm
) -> int:
    qubits = sorted(operator_with_largest_expval.qubits)
    # term_with_largest_expval is now a subscriptable tuple like ((0, 'Z'), (1, 'Z'))
    # In order of increasing qubit number

    qubit_to_get_rid_of: int = qubits[1]  # the larger qubit number
    qubit_itll_be_replaced_with: int = qubits[0]  # the smaller qubit number

    new_indice = old_indice

    if old_indice > qubit_to_get_rid_of:
        # map qubit to the qubit 1 below it
        new_indice = old_indice - 1
    elif old_indice == qubit_to_get_rid_of:
        # map qubit onto the qubit it's being replaced with
        new_indice = qubit_itll_be_replaced_with

    return new_indice


def _create_reduced_hamiltonian(
    hamiltonian: PauliRepresentation,
    term_with_largest_expval: PauliTerm,
    largest_expval: float,
) -> PauliSum:
    """Reduce the cost hamiltonian accordinmg to eq. (15) in the original paper.

    Reduction is done by substituting one qubit of the term with the largest
    expectation value with the other qubit of the term. See equation (15) of the
    original paper.

    Args:
        hamiltonian: hamiltonian to be reduced
        term_with_largest_expval: term with largest expectation value
        largest_expval: the expectation value of `term_with_largest_expval`

    Returns:
        Reduced hamiltonian.
    """
    reduced_hamiltonian = PauliSum()

    # Get rid of the larger qubit in the term.
    qubit_to_get_rid_of = sorted(term_with_largest_expval.qubits)[-1]

    for term in hamiltonian.terms:
        coefficient = term.coefficient
        if term != term_with_largest_expval:
            # If term is not the term_with_largest_expval
            new_term_strs = []
            for qubit_indice in term.qubits:

                # Map the new cost hamiltonian onto reduced qubits
                new_qubit_indice = _get_new_qubit_indice(
                    qubit_indice, term_with_largest_expval
                )
                new_term_strs.append(f"Z{new_qubit_indice}")

                if qubit_indice == qubit_to_get_rid_of:
                    coefficient *= np.sign(largest_expval)

            reduced_hamiltonian += PauliTerm("*".join(new_term_strs), coefficient)

    return reduced_hamiltonian


def _map_reduced_solutions_to_original_solutions(
    reduced_solutions: List[Tuple[int, ...]], qubit_map: Dict[int, List[int]]
):
    """Map the answer of the reduced Hamiltonian back to the original number of qubits.

    Args:
        reduced_solutions: list of solutions, each solution is a tuple of ints.
        qubit_map: list that maps original qubits to new qubits, see docstring of
            _update_qubit_map for more details.

    Returns:
        list of solutions, each solution is a tuple of ints.
    """

    original_solutions: List[Tuple[int, ...]] = []

    for reduced_solution in reduced_solutions:
        original_solution: List[int] = []
        for qubit, sign in qubit_map.values():
            this_answer = reduced_solution[qubit]

            # If negative, flip the qubit.
            if sign == -1:
                if this_answer == 0:
                    this_answer = 1
                else:
                    this_answer = 0
            original_solution.append(this_answer)

        original_solutions.append(tuple(original_solution))

    return original_solutions
