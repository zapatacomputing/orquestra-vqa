import pytest
from openfermion import QubitOperator, IsingOperator

from zquantum.core.symbolic_simulator import SymbolicSimulator
from zquantum.core.circuits import Circuit, X, H
from zquantum.qaoa.estimators import CvarEstimator

from zquantum.core.interfaces.estimation import EstimationTask


class TestCvarEstimator:
    @pytest.fixture(
        params=[
            {
                "alpha": 0.2,
                "use_exact_expectation_values": False,
            },
            {
                "alpha": 0.2,
                "use_exact_expectation_values": True,
            },
            {
                "alpha": 0.8,
                "use_exact_expectation_values": False,
            },
            {
                "alpha": 0.8,
                "use_exact_expectation_values": True,
            },
            {
                "alpha": 1.0,
                "use_exact_expectation_values": False,
            },
            {
                "alpha": 1.0,
                "use_exact_expectation_values": True,
            },
        ]
    )
    def estimator(self, request):
        return CvarEstimator(**request.param)

    @pytest.fixture()
    def circuit(self):
        return Circuit([X(0)])

    @pytest.fixture()
    def operator(self):
        return IsingOperator("Z0")

    @pytest.fixture()
    def estimation_tasks(self, operator, circuit):
        return [EstimationTask(operator, circuit, 10)]

    @pytest.fixture()
    def backend(self):
        return SymbolicSimulator()

    def test_raises_exception_if_operator_is_not_ising(
        self, estimator, backend, circuit
    ):
        # Given
        estimation_tasks = [EstimationTask(QubitOperator("X0"), circuit, 10)]
        with pytest.raises(TypeError):
            estimator(
                backend=backend,
                estimation_tasks=estimation_tasks,
            )

    def test_cvar_estimator_raises_exception_if_alpha_less_than_0(
        self, estimator, backend, estimation_tasks
    ):
        estimator.alpha = -1
        with pytest.raises(ValueError):
            estimator(
                backend=backend,
                estimation_tasks=estimation_tasks,
            )

    def test_cvar_estimator_raises_exception_if_alpha_greater_than_1(
        self, estimator, backend, estimation_tasks
    ):
        estimator.alpha = 2
        with pytest.raises(ValueError):
            estimator(
                backend=backend,
                estimation_tasks=estimation_tasks,
            )

    def test_cvar_estimator_returns_correct_values(self, estimator, backend, operator):
        # Given
        estimation_tasks = [EstimationTask(operator, Circuit([H(0)]), 10000)]
        if estimator.alpha <= 0.5:
            target_value = -1
        else:
            target_value = (-1 * 0.5 + 1 * (estimator.alpha - 0.5)) / estimator.alpha

        # When
        expectation_values = estimator(
            backend=backend,
            estimation_tasks=estimation_tasks,
        )

        # Then
        assert len(expectation_values) == len(estimation_tasks)
        assert expectation_values[0].values == pytest.approx(target_value, abs=2e-1)
