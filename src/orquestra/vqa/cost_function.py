from zquantum.core.utils import ValueEstimate

from .interfaces.backend import QuantumBackend
from .interfaces.ansatz import Ansatz
from .interfaces.estimator import Estimator
from .circuit import combine_ansatz_params
from .estimator import BasicEstimator
from .measurement import ExpectationValues
from typing import Optional
import numpy as np
from openfermion import SymbolicOperator


def sum_expectation_values(expectation_values: ExpectationValues) -> ValueEstimate:
    """Compute the sum of expectation values.

    If correlations are available, the precision of the sum is computed as

    \epsilon = \sqrt{\sum_k \sigma^2_k}

    where the sum runs over frames and \sigma^2_k is the estimated variance of
    the estimated contribution of frame k to the total. This is calculated as

    \sigma^2_k = \sum_{i,j} Cov(o_{k,i}, o_{k, j})

    where Cov(o_{k,i}, o_{k, j}) is the estimated covariance in the estimated
    expectation values of operators i and j of frame k.

    Args:
        expectation_values: The expectation values to sum.
    
    Returns:
        The value of the sum, including a precision if the expectation values
            included covariances.

    """

    value = np.sum(expectation_values)

    precision = None

    if expectation_values.covariances:
        variance = 0
        frame_begin_index = 0
        for frame_covariance in expectation_values.covariances:
            num_terms = frame_covariance.shape[0]
            for i in range(num_terms):
                value_i = expectation_values.values[frame_begin_index + i]
                for j in range(num_terms):
                    value_j = expectation_values.values[frame_begin_index + j]
                    variance += frame_covariance[i, j] - value_i * value_j

            frame_begin_index += num_terms
        precision = np.sqrt(variance)

    return ValueEstimate(value, precision)


class AnsatzBasedCostFunction:
    """Cost function used for evaluating given operator using given ansatz.

    Args:
        target_operator (openfermion.QubitOperator): operator to be evaluated
        ansatz (zquantum.core.interfaces.ansatz.Ansatz): ansatz used to evaluate cost function
        backend (zquantum.core.interfaces.backend.QuantumBackend): backend used for evaluation
        estimator: (zquantum.core.interfaces.estimator.Estimator) = estimator used to compute expectation value of target operator
        n_samples (int): number of samples (i.e. measurements) to be used in the estimator. 
        epsilon (float): an additive/multiplicative error term. The cost function should be computed to within this error term. 
        delta (float): a confidence term. If theoretical upper bounds are known for the estimation technique, 
            the final estimate should be within the epsilon term, with probability 1 - delta.
        fixed_parameters (np.ndarray): values for the circuit parameters that should be fixed. 
        parameter_precision (float): the standard deviation of the Gaussian noise to add to each parameter, if any.

    Params:
        target_operator (openfermion.QubitOperator): see Args
        ansatz (zquantum.core.interfaces.ansatz.Ansatz): see Args
        backend (zquantum.core.interfaces.backend.QuantumBackend): see Args
        estimator: (zquantum.core.interfaces.estimator.Estimator) = see Args 
        n_samples (int): see Args
        epsilon (float): see Args
        delta (float): see Args
        fixed_parameters (np.ndarray): see Args
        parameter_precision (float): see Args
    """

    def __init__(
        self,
        target_operator: SymbolicOperator,
        ansatz: Ansatz,
        backend: QuantumBackend,
        estimator: Estimator = None,
        n_samples: Optional[int] = None,
        epsilon: Optional[float] = None,
        delta: Optional[float] = None,
        fixed_parameters: Optional[np.ndarray] = None,
        parameter_precision: Optional[float] = None,
        parameter_precision_seed: Optional[int] = None,
    ):
        self.target_operator = target_operator
        self.ansatz = ansatz
        self.backend = backend
        if estimator is None:
            self.estimator = BasicEstimator()
        else:
            self.estimator = estimator
        self.n_samples = n_samples
        self.epsilon = epsilon
        self.delta = delta
        self.fixed_parameters = fixed_parameters
        self.parameter_precision = parameter_precision
        self.parameter_precision_seed = parameter_precision_seed

    def __call__(self, parameters: np.ndarray) -> ValueEstimate:
        """Evaluates the value of the cost function for given parameters.

        Args:
            parameters: parameters for which the evaluation should occur.

        Returns:
            value: cost function value for given parameters.
        """
        full_parameters = parameters.copy()
        if self.fixed_parameters is not None:
            full_parameters = combine_ansatz_params(self.fixed_parameters, parameters)
        if self.parameter_precision is not None:
            rng = np.random.default_rng(self.parameter_precision_seed)
            noise_array = rng.normal(
                0.0, self.parameter_precision, len(full_parameters)
            )
            full_parameters += noise_array

        circuit = self.ansatz.get_executable_circuit(full_parameters)
        expectation_values = self.estimator.get_estimated_expectation_values(
            self.backend,
            circuit,
            self.target_operator,
            n_samples=self.n_samples,
            epsilon=self.epsilon,
            delta=self.delta,
        )
        precision = None

        return sum_expectation_values(expectation_values)
