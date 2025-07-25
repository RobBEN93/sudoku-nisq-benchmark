import math
import mpmath
from copy import deepcopy
from pytket import Circuit, Qubit, OpType
from typing import Literal
from sudoku_nisq.exact_cover_encoding import ExactCoverEncoding
from sudoku_nisq.quantum_solver import QuantumSolver

class ExactCoverQuantumSolver(QuantumSolver):
    """
    This module defines the ExactCoverQuantumSolver class, which constructs and simulates or
    runs a quantum circuit through a backend to solve any Exact Cover problem using 
    using an algorithm based on Grover's algorithm: 
        J. -R. Jiang and Y. -J. Wang, "Quantum Circuit Based on Grover’s Algorithm to Solve Exact Cover Problem," 2023
        VTS Asia Pacific Wireless Communications Symposium (APWCS), Tainan city, Taiwan, 2023, pp. 1-5, doi: 10.1109/APWCS60142.2023.10234054.

    Usage:
    - Initialize the solver with the problem instance.
    - Build the circuit using build_circuit().

    Example:
    # Define your problem instance
            puzzle = SudokuPuzzle()
    
    # Initialize the solver
    solver = ExactCoverQuantumSolver(puzzle)

    # Build the quantum circuit
    circuit = solver.build_circuit()
    
    """  
    def __init__(self, puzzle=None, metadata_manager=None, encoding: Literal["simple", "pattern"] = "simple",
                 num_solutions=None, universe=None, subsets=None, **kwargs):
        """
        Initialize the ExactCoverQuantumSolver instance.

        Parameters:
        - puzzle: The Sudoku puzzle to solve.
        - metadata_manager: MetadataManager instance for caching and logging.
        - encoding: Which encoding to use ("simple" or "pattern").
        - num_solutions: If you want to limit # of classical solutions extracted.
        - universe: Custom universe for exact cover (optional).
        - subsets: Custom subsets for exact cover (optional).
        - **kwargs: Additional parameters passed to QuantumSolver base class

        encoding:  "simple"  → use the simple one-hot subsets,
                   "pattern" → use the pattern-based subsets.
        num_solutions:  if you want to limit # of classical solutions extracted
        """
        # Initialize the base class with all parameters
        super().__init__(
            puzzle=puzzle,
            metadata_manager=metadata_manager,
            encoding=encoding,
            **kwargs
        )
                    
        # Initialize encoding
        enc = ExactCoverEncoding(puzzle)
        self.universe = enc.universe
        
        # Determine which encoding to use
        if encoding == "simple":
            self.subsets = enc.simple_subsets
        elif encoding == "pattern":
            self.subsets = enc.pattern_subsets
        else:
            raise ValueError(f"Unknown encoding {encoding!r}")
        
        if num_solutions is None:
            self.num_solutions = puzzle.num_solutions
            
        self.u_size = len(self.universe)        # Total elements to cover
        self.s_size = len(self.subsets)              # Number of subsets
        self.b = math.ceil(math.log2(self.s_size))  # Qubits for counting

    def resource_estimation(self):
        s_size = self.s_size
        num_solutions = self.num_solutions

        # Set the decimal precision
        mpmath.mp.dps = 50  # Adjust as needed for precision

        # Compute logarithms to avoid large numbers
        ln_2 = mpmath.log(2)
        ln_pi_over_4 = mpmath.log(mpmath.pi / 4)
        ln_num_solutions = mpmath.log(num_solutions)

        # Calculate ln_a
        ln_a = (s_size * ln_2 - ln_num_solutions) / 2

        # Calculate ln_num_iterations
        ln_num_iterations = ln_pi_over_4 + ln_a

        # Compute num_iterations without overflow
        num_iterations = int(mpmath.floor(mpmath.exp(ln_num_iterations)))

        # Calculate the number of qubits
        num_qubits = self.s_size + self.u_size * self.b + 1
        
        # Gate counts
        superpos_gates = self.s_size
        prepare_anc_gates = 2
        counter_gates = 0
        for s in self.subsets:
            counter_gates += len(self.subsets[s]) * self.b
        oracle_gates = 1 + 2 * ((self.u_size - 1) * self.b)
        diffuser_gates = 1 + 4 * self.s_size
        MCX_gates = num_iterations * (oracle_gates + 2 * counter_gates)
        total_gates = (superpos_gates + prepare_anc_gates +
                    MCX_gates + num_iterations * diffuser_gates)
        return {
            "n_qubits": num_qubits,
            "MCX_gates": MCX_gates,
            "n_gates": total_gates,
            "depth": None  # Depth is not calculated here
        }

    def _build_circuit(self):
        """
        Builds and returns the full quantum circuit for the Exact Cover problem.

        Returns:
        - self.main_circuit (Circuit): The constructed quantum circuit.
        
        """
        # Returns the fully assembled circuit
                # Initialize circuits
        self.main_circuit = Circuit()
        self.oracle = Circuit()
        self.diffuser = Circuit()
        self.count_circuit = Circuit()
        self.count_circuit_dag = Circuit()
        self.aux_circ = Circuit()

        # Generate a register for the subsets
        self.s_qubits = [Qubit("S",i) for i in range(self.s_size)]

        # Add subset qubits to the main circuit
        for q in self.s_qubits:
            self.main_circuit.add_qubit(q)

        # Apply Hadamard to each qubit in the main circuit
        for q in self.s_qubits:
            self.main_circuit.H(q)

        # Add the subset register to the counting, diffuser and auxiliary circuits
        for q in self.s_qubits:
            self.count_circuit.add_qubit(q)
            self.diffuser.add_qubit(q)
            self.aux_circ.add_qubit(q)
        
        '''
        For each element u_i in U, we add qubits U_i[0], ... , U_i[b] for implementing
        the counter of the element u_i to store the number of subsets covering it
        '''
        self.u_qubits = []
        for i in range(self.u_size):
            label = f"U_{i}"
            u_label_qubits = [Qubit(label,j) for j in range(self.b)]
            self.u_qubits.extend(u_label_qubits)

        # Add the U_{i} registers to the main, counting, oracle and auxiliary circuits
        for q in self.u_qubits:
            self.main_circuit.add_qubit(q)
            self.count_circuit.add_qubit(q)
            self.oracle.add_qubit(q)
            self.aux_circ.add_qubit(q)

        # Add the ancilla
        self.anc = Qubit("anc")
        self.main_circuit.add_qubit(self.anc)
        self.oracle.add_qubit(self.anc)
        self.aux_circ.add_qubit(self.anc)
        self.main_circuit.add_gate(OpType.X, [self.anc])
        self.main_circuit.add_gate(OpType.H, [self.anc])
        
        self._assemble_full_circuit_w_meas()
        return self.main_circuit

    def _build_counter(self):
        """
        Constructs the counting circuit that counts the number of subsets covering each element.

        For each element u_i in U, we have a set of qubits U_i[0], ..., U_i[b-1] used to count
        the number of subsets covering u_i.

        The counting is performed using controlled increment operations based on the S qubits
        and the subsets they represent.
        """
    
        ## Generate lists for creating the MCX gates for the counters
        # The following section takes a subset and creates a list of the qubits that will be
        # controls and targets for the MCX gate for generating the counters
        
        # Initialize list to store the MCX gate qubit lists
        all_lists = []  # This will store all generated lists
        j = 0  # Index for the S qubit corresponding to the S_j subset
        for subset in self.subsets:
            q_list = []
            for elementU in self.subsets[subset]:
                S_list = []  # Generate a list to contain all lists of qubits to add a MCX
                S_list.append(Qubit("S", j))
                # Access register corresponding to the element u_i in subset S_subset
                i = self.universe.index(elementU)
                label = f"U_{i}"
                register = [q for q in self.count_circuit.qubits if q.reg_name.startswith(label)]
                for q in register:
                    S_list.append(q)
                    q_list.append(deepcopy(S_list))
            all_lists.append(q_list)
            j += 1
        
        # We reverse the list because of the construction in the previous step leaves them in 
        # reverse order
        reversed_lists = []
        for element in all_lists:
            reversed_element = element[::-1]
            reversed_lists.append(reversed_element)
        
        # Add the MCX gates to the counting circuit
        for element in reversed_lists:
            for q_list in element:
                self.count_circuit.add_gate(OpType.CnX, q_list)

        # Create the dagger (inverse) of the counting circuit
        self.count_circuit_dag = self.count_circuit.dagger()

    def _build_oracle(self):
        """
        Constructs the diffuser circuit used in Grover's algorithm.
        """
        
        # Apply X gates to all U qubits except those corresponding to zero
        for q in self.u_qubits:
            if q.index[0] != 0:
                    self.oracle.X(q)
                    
        # Prepare the list of qubits for the multi-controlled X gate
        oracle_qubits_list = []
        for q in self.u_qubits:
            oracle_qubits_list.append(q)
        oracle_qubits_list.append(self.anc)
        self.oracle.add_gate(OpType.CnX, oracle_qubits_list)
        
        # Apply X gates again to revert the qubits
        for q in self.u_qubits:
            if q.index[0] != 0:
                    self.oracle.X(q)

    def _build_diffuser(self):
        """
        Constructs the diffuser circuit used in Grover's algorithm.
        """
        diffuser_qubits_list = []
        for q in self.s_qubits:
            self.diffuser.H(q)
            self.diffuser.X(q)
            diffuser_qubits_list.append(q)
        
        self.diffuser.add_gate(OpType.CnZ, diffuser_qubits_list)
        
        for q in self.s_qubits:
            self.diffuser.X(q)
            self.diffuser.H(q)

    def _assemble_aux_circ(self):
        """
        Assembles the auxiliary circuit which includes the counter and oracle.
        """
        self._build_counter()
        self._build_oracle()
        self.aux_circ.append(self.count_circuit)
        self.aux_circ.append(self.oracle)
        self.aux_circ.append(self.count_circuit_dag)

    def _assemble_full_circuit_w_meas(self, num_iterations = None):
        """
        Assembles the full circuit including auxiliary circuits and measurements.

        Parameters:
        - num_iterations (int): Number of Grover iterations. If None, it is calculated automatically.
        """
        self._assemble_aux_circ()
        self._build_diffuser()
        
        if num_iterations is None:
            num_iterations = math.floor((math.pi / 4) * math.sqrt((2 ** self.s_size) / self.num_solutions))
        
        # Append sub-circuits to the main circuit
        for i in range(num_iterations):
            self.main_circuit.append(self.aux_circ)
            self.main_circuit.append(self.diffuser)

        c_bits = self.main_circuit.add_c_register("c", self.s_size)
        for q in self.s_qubits:
            self.main_circuit.Measure(q, c_bits[q.index[0]])