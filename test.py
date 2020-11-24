"""Core code to be used for scheduling a task DAG with HEFT"""
from collections import deque, namedtuple
from math import inf
from heft.gantt import showGanttChart
from types import SimpleNamespace
from enum import Enum

import argparse
import logging
import sys
import numpy as np
import matplotlib.pyplot as plt
import networkx as nx

logger = logging.getLogger('heft')

ScheduleEvent = namedtuple('ScheduleEvent', 'task start end proc wf_id')
Workflows = namedtuple('Workflows', 'dag computation_matrix comp comm id')
Node_sequence = namedtuple('Node_sequence', 'wf_id node_id')
"""
Default computation matrix - taken from Topcuoglu 2002 HEFT paper
computation matrix: v x q matrix with v tasks and q PEs
"""
W0 = np.array([
    [14, 16, 9],
    [13, 19, 18],
    [11, 13, 19],
    [13, 8, 17],
    [12, 13, 10],
    [13, 16, 9],
    [7, 15, 11],
    [5, 11, 14],
    [18, 12, 20],
    [21, 7, 16]
])

"""
Default communication matrix - not listed in Topcuoglu 2002 HEFT paper
communication matrix: q x q matrix with q PEs

Note that a communication cost of 0 is used for a given processor to itself
"""
C0 = np.array([
    [0, 1, 1],
    [1, 0, 1],
    [1, 1, 0]
])

"""
Default communication startup cost vector
"""
L0 = np.array([0, 0, 0])

class RankMetric(Enum):
    MEAN = "MEAN"
    WORST = "WORST"
    BEST = "BEST"
    EDP = "EDP"

class OpMode(Enum):
    EFT = "EFT"
    EDP_REL = "EDP RELATIVE"
    EDP_ABS = "EDP ABSOLUTE"
    ENERGY = "ENERGY"

class Strategies(Enum):
    S_COMP = "S_COMP"
    L_COMP = "L_COMP"
    S_COMM = "S_COMM"
    L_COMM = "L_COMM"
    S_RANK = "S_RANK"
    L_RANK = "L_RANK"
    LEVEL = "LEVEL"
    RAND = "RAND"
    L_SQUE = "L_SQUE"
    S_SQUE = "S_SQUE"
    HEFT = "HEFT"

key = None
reverse = True
strategies = Strategies.S_RANK

def schedule_dag(dag, computation_matrix=W0, communication_matrix=C0, communication_startup=L0, proc_schedules=None, time_offset=0, relabel_nodes=True, rank_metric=RankMetric.MEAN, **kwargs):
    """
    Given an application DAG and a set of matrices specifying PE bandwidth and (task, pe) execution times, computes the HEFT schedule
    of that DAG onto that set of PEs 
    """
    if proc_schedules == None:
        proc_schedules = {}

    _self = {
        'computation_matrix': computation_matrix,
        'communication_matrix': communication_matrix,
        'communication_startup': communication_startup,
        'task_schedules': {},
        'proc_schedules': proc_schedules,
        'numExistingJobs': 0,
        'time_offset': time_offset,
        'root_node': None
    }
    _self = SimpleNamespace(**_self)

    for proc in proc_schedules:
        _self.numExistingJobs = _self.numExistingJobs + len(proc_schedules[proc])

    if relabel_nodes:
        dag = nx.relabel_nodes(dag, dict(map(lambda node: (node, node+_self.numExistingJobs), list(dag.nodes()))))
    else:
        #Negates any offsets that would have been needed had the jobs been relabeled
        _self.numExistingJobs = 0

    for i in range(_self.numExistingJobs + len(_self.computation_matrix)):
        _self.task_schedules[i] = None
    for i in range(len(_self.communication_matrix)):
        if i not in _self.proc_schedules:
            _self.proc_schedules[i] = []

    for proc in proc_schedules:
        for schedule_event in proc_schedules[proc]:
            _self.task_schedules[schedule_event.task] = schedule_event

    # Nodes with no successors cause the any expression to be empty  
    # 找到entry node  
    root_node = [node for node in dag.nodes() if not any(True for _ in dag.predecessors(node))] # 没有任何前驱节点
    assert len(root_node) == 1, f"Expected a single root node, found {len(root_node)}"
    root_node = root_node[0]
    _self.root_node = root_node
 
    # 计算向上排序值
    logger.debug(""); logger.debug("====================== Performing Rank-U Computation ======================\n"); logger.debug("")
    _compute_ranku(_self, dag, metric=rank_metric, **kwargs)

    logger.debug(""); logger.debug("====================== Computing EFT for each (task, processor) pair and scheduling in order of decreasing Rank-U ======================"); logger.debug("")
    sorted_nodes = sorted(dag.nodes(), key=lambda node: dag.nodes()[node]['ranku'], reverse=True)
    if sorted_nodes[0] != root_node:
        logger.debug("Root node was not the first node in the sorted list. Must be a zero-cost and zero-weight placeholder node. Rearranging it so it is scheduled first\n")
        idx = sorted_nodes.index(root_node)
        sorted_nodes[idx], sorted_nodes[0] = sorted_nodes[0], sorted_nodes[idx]



    # 按照顺序为每个node分配处理器
    for node in sorted_nodes:
        if _self.task_schedules[node] is not None:
            continue
        wf_id = computation_matrix[node - _self.numExistingJobs][-1]
        minTaskSchedule = ScheduleEvent(node, inf, inf, -1, 0)
        minEDP = inf
        op_mode = kwargs.get("op_mode", OpMode.EFT)
        if op_mode == OpMode.EDP_ABS:
            assert "power_dict" in kwargs, "In order to perform EDP-based processor assignment, a power_dict is required"
            taskschedules = []
            minScheduleStart = inf

            for proc in range(len(communication_matrix)):
                taskschedule = _compute_eft(_self, dag, node, proc, wf_id)
                edp_t = ((taskschedule.end - taskschedule.start)**2) * kwargs["power_dict"][node][proc]
                if (edp_t < minEDP):
                    minEDP = edp_t
                    minTaskSchedule = taskschedule
                elif (edp_t == minEDP and taskschedule.end < minTaskSchedule.end):
                    minTaskSchedule = taskschedule
        
        elif op_mode == OpMode.EDP_REL:
            assert "power_dict" in kwargs, "In order to perform EDP-based processor assignment, a power_dict is required"
            taskschedules = []
            minScheduleStart = inf

            for proc in range(len(communication_matrix)):
                taskschedules.append(_compute_eft(_self, dag, node, proc, wf_id))
                if taskschedules[proc].start < minScheduleStart:
                    minScheduleStart = taskschedules[proc].start

            for taskschedule in taskschedules:
                # Use the makespan relative to the earliest potential assignment to encourage load balancing
                edp_t = ((taskschedule.end - minScheduleStart)**2) * kwargs["power_dict"][node][taskschedule.proc]
                if (edp_t < minEDP):
                    minEDP = edp_t
                    minTaskSchedule = taskschedule
                elif (edp_t == minEDP and taskschedule.end < minTaskSchedule.end):
                    minTaskSchedule = taskschedule
        
        elif op_mode == OpMode.ENERGY:
            assert False, "Feature not implemented"
            assert "power_dict" in kwargs, "In order to perform Energy-based processor assignment, a power_dict is required"
        # EFT
        else: 
            for proc in range(len(communication_matrix)):
                taskschedule = _compute_eft(_self, dag, node, proc, wf_id)
                if (taskschedule.end < minTaskSchedule.end):
                    minTaskSchedule = taskschedule
        
        # 为节点分配最优处理器
        _self.task_schedules[node] = minTaskSchedule
        # 相应处理器的相应时间段被占用
        _self.proc_schedules[minTaskSchedule.proc].append(minTaskSchedule)
        # 已经被使用的时间段排序
        _self.proc_schedules[minTaskSchedule.proc] = sorted(_self.proc_schedules[minTaskSchedule.proc], key=lambda schedule_event: (schedule_event.end, schedule_event.start))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('\n')
            for proc, jobs in _self.proc_schedules.items():
                logger.debug(f"Processor {proc} has the following jobs:")
                logger.debug(f"\t{jobs}")
            logger.debug('\n')
        # 保证相同处理器的前一个任务结束时间大于下一个任务的开始时间
        for proc in range(len(_self.proc_schedules)):
            for job in range(len(_self.proc_schedules[proc])-1):
                first_job = _self.proc_schedules[proc][job]
                second_job = _self.proc_schedules[proc][job+1]
                assert first_job.end <= second_job.start, \
                f"Jobs on a particular processor must finish before the next can begin, but job {first_job.task} on processor {first_job.proc} ends at {first_job.end} and its successor {second_job.task} starts at {second_job.start}"
    # dict_output 任务id: （处理器，在此处理器上的第几个任务，上一个任务）
    dict_output = {}
    for proc_num, proc_tasks in _self.proc_schedules.items():
        for idx, task in enumerate(proc_tasks):
            if idx > 0 and (proc_tasks[idx-1].end - proc_tasks[idx-1].start > 0):
                dict_output[task.task] = (proc_num, idx, [proc_tasks[idx-1].task])
            else:
                dict_output[task.task] = (proc_num, idx, [])

    return _self.proc_schedules, _self.task_schedules, dict_output

def _scale_by_operating_freq(_self, **kwargs):
    if "operating_freqs" not in kwargs:
        logger.debug("No operating frequency argument is present, assuming at max frequency and values are unchanged")
        return
    return #TODO
    #for pe_num, freq in enumerate(kwargs["operating_freqs"]):
        #_self.computation_matrix[:, pe_num] = _self.computation_matrix[:, pe_num] * (1 + compute_DVFS_performance_slowdown(pe_num, freq))

def _compute_ranku(_self, dag, metric=RankMetric.MEAN, **kwargs):
    """
    Uses a basic BFS approach to traverse upwards through the graph assigning ranku along the way
    """
    terminal_node = [node for node in dag.nodes() if not any(True for _ in dag.successors(node))]
    assert len(terminal_node) == 1, f"Expected a single terminal node, found {len(terminal_node)}"
    terminal_node = terminal_node[0]

    #TODO: Should this be configurable?
    #avgCommunicationCost = np.mean(_self.communication_matrix[np.where(_self.communication_matrix > 0)])
    diagonal_mask = np.ones(_self.communication_matrix.shape, dtype=bool)
    np.fill_diagonal(diagonal_mask, 0)
    avgCommunicationCost = np.mean(_self.communication_matrix[diagonal_mask]) + np.mean(_self.communication_startup)
    for edge in dag.edges():
        logger.debug(f"Assigning {edge}'s average weight based on average communication cost. {float(dag.get_edge_data(*edge)['weight'])} => {float(dag.get_edge_data(*edge)['weight']) / avgCommunicationCost}")
        nx.set_edge_attributes(dag, { edge: float(dag.get_edge_data(*edge)['weight']) / avgCommunicationCost }, 'avgweight')

    # Utilize a masked array so that np.mean, etc, calculations ignore the entries that are inf
    comp_matrix_masked = np.ma.masked_where(_self.computation_matrix == inf, _self.computation_matrix)

    nx.set_node_attributes(dag, { terminal_node: np.mean(comp_matrix_masked[terminal_node-_self.numExistingJobs]) }, "ranku")
    visit_queue = deque(dag.predecessors(terminal_node))

    while visit_queue:
        node = visit_queue.pop()
        while _node_can_be_processed(_self, dag, node) is not True:
            try:
                node2 = visit_queue.pop()
            except IndexError:
                raise RuntimeError(f"Node {node} cannot be processed, and there are no other nodes in the queue to process instead!")
            visit_queue.appendleft(node)
            node = node2

        logger.debug(f"Assigning ranku for node: {node}")
        if metric == RankMetric.MEAN:
            max_successor_ranku = -1
            for succnode in dag.successors(node):
                logger.debug(f"\tLooking at successor node: {succnode}")
                logger.debug(f"\tThe edge weight from node {node} to node {succnode} is {dag[node][succnode]['avgweight']}, and the ranku for node {node} is {dag.nodes()[succnode]['ranku']}")
                val = float(dag[node][succnode]['avgweight']) + dag.nodes()[succnode]['ranku']
                if val > max_successor_ranku:
                    max_successor_ranku = val
            assert max_successor_ranku >= 0, f"Expected maximum successor ranku to be greater or equal to 0 but was {max_successor_ranku}"
            nx.set_node_attributes(dag, { node: np.mean(comp_matrix_masked[node-_self.numExistingJobs]) + max_successor_ranku }, "ranku")
        
        elif metric == RankMetric.WORST:
            max_successor_ranku = -1
            max_node_idx = np.where(comp_matrix_masked[node-_self.numExistingJobs] == max(comp_matrix_masked[node-_self.numExistingJobs]))[0][0]
            logger.debug(f"\tNode {node} has maximum computation cost of {comp_matrix_masked[node-_self.numExistingJobs][max_node_idx]} on processor {max_node_idx}")
            for succnode in dag.successors(node):
                logger.debug(f"\tLooking at successor node: {succnode}")
                max_succ_idx = np.where(comp_matrix_masked[succnode-_self.numExistingJobs] == max(comp_matrix_masked[succnode-_self.numExistingJobs]))[0][0]
                logger.debug(f"\tNode {succnode} has maximum computation cost of {comp_matrix_masked[succnode-_self.numExistingJobs][max_succ_idx]} on processor {max_succ_idx}")
                val = _self.communication_matrix[max_node_idx, max_succ_idx] + dag.nodes()[succnode]['ranku']
                if val > max_successor_ranku:
                    max_successor_ranku = val
            assert max_successor_ranku >= 0, f"Expected maximum successor ranku to be greater or equal to 0 but was {max_successor_ranku}"
            nx.set_node_attributes(dag, { node: comp_matrix_masked[node-_self.numExistingJobs, max_node_idx] + max_successor_ranku}, "ranku")
        
        elif metric == RankMetric.BEST:
            min_successor_ranku = inf
            min_node_idx = np.where(comp_matrix_masked[node-_self.numExistingJobs] == min(comp_matrix_masked[node-_self.numExistingJobs]))[0][0]
            logger.debug(f"\tNode {node} has minimum computation cost on processor {min_node_idx}")
            for succnode in dag.successors(node):
                logger.debug(f"\tLooking at successor node: {succnode}")
                min_succ_idx = np.where(comp_matrix_masked[succnode-_self.numExistingJobs] == min(comp_matrix_masked[succnode-_self.numExistingJobs]))[0][0]
                logger.debug(f"\tThis successor node has minimum computation cost on processor {min_succ_idx}")
                val = _self.communication_matrix[min_node_idx, min_succ_idx] + dag.nodes()[succnode]['ranku']
                if val < min_successor_ranku:
                    min_successor_ranku = val
            assert min_successor_ranku >= 0, f"Expected minimum successor ranku to be greater or equal to 0 but was {min_successor_ranku}"
            nx.set_node_attributes(dag, { node: comp_matrix_masked[node-_self.numExistingJobs, min_node_idx] + min_successor_ranku}, "ranku")
        
        elif metric == RankMetric.EDP:
            assert "power_dict" in kwargs, "In order to perform EDP-based Rank Method, a power_dict is required"
            power_dict = kwargs.get("power_dict", np.array([[]]))
            power_dict_masked = np.ma.masked_where(power_dict[node] == inf, power_dict[node])
            max_successor_ranku = -1
            for succnode in dag.successors(node):
                logger.debug(f"\tLooking at successor node: {succnode}")
                logger.debug(f"\tThe edge weight from node {node} to node {succnode} is {dag[node][succnode]['avgweight']}, and the ranku for node {node} is {dag.nodes()[succnode]['ranku']}")
                val = float(dag[node][succnode]['avgweight']) + dag.nodes()[succnode]['ranku']
                if val > max_successor_ranku:
                    max_successor_ranku = val
            assert max_successor_ranku >= 0, f"Expected maximum successor ranku to be greater or equal to 0 but was {max_successor_ranku}"
            avg_edp = np.mean(comp_matrix_masked[node-_self.numExistingJobs])**2 * np.mean(power_dict_masked)
            nx.set_node_attributes(dag, { node: avg_edp + max_successor_ranku }, "ranku")
        
        else:
            raise RuntimeError(f"Unrecognied Rank-U metric {metric}, unable to compute upward rank")

        visit_queue.extendleft([prednode for prednode in dag.predecessors(node) if prednode not in visit_queue])
    
    logger.debug("")
    for node in dag.nodes():
        logger.debug(f"Node: {node}, Rank U: {dag.nodes()[node]['ranku']}")

def _node_can_be_processed(_self, dag, node):
    """
    Validates that a node is able to be processed in Rank U calculations. Namely, that all of its successors have their Rank U values properly assigned
    Otherwise, errors can occur in processing DAGs of the form
    A
    |\
    | B
    |/
    C
    Where C enqueues A and B, A is popped off, and it is unable to be processed because B's Rank U has not been computed
    """
    for succnode in dag.successors(node):
        if 'ranku' not in dag.nodes()[succnode]:
            logger.debug(f"Attempted to compute the Rank U for node {node} but found that it has an unprocessed successor {dag.nodes()[succnode]}. Will try with the next node in the queue")
            return False
    return True

def _compute_eft(_self, dag, node, proc, wf_id):
    """
    Computes the EFT of a particular node if it were scheduled on a particular processor
    It does this by first looking at all predecessor tasks of a particular node and determining the earliest time a task would be ready for execution (ready_time)
    It then looks at the list of tasks scheduled on this particular processor and determines the earliest time (after ready_time) a given node can be inserted into this processor's queue
    """
    ready_time = _self.time_offset
    logger.debug(f"Computing EFT for node {node} on processor {proc}")
    for prednode in list(dag.predecessors(node)):
        predjob = _self.task_schedules[prednode]
        assert predjob != None, f"Predecessor nodes must be scheduled before their children, but node {node} has an unscheduled predecessor of {prednode}"
        logger.debug(f"\tLooking at predecessor node {prednode} with job {predjob} to determine ready time")
        if _self.communication_matrix[predjob.proc, proc] == 0:
            ready_time_t = predjob.end
        else:
            ready_time_t = predjob.end + dag[predjob.task][node]['weight'] / _self.communication_matrix[predjob.proc, proc] + _self.communication_startup[predjob.proc]
        logger.debug(f"\tNode {prednode} can have its data routed to processor {proc} by time {ready_time_t}")
        if ready_time_t > ready_time:
            ready_time = ready_time_t
    logger.debug(f"\tReady time determined to be {ready_time}")

    computation_time = _self.computation_matrix[node-_self.numExistingJobs, proc]
    job_list = _self.proc_schedules[proc]
    for idx in range(len(job_list)):
        prev_job = job_list[idx]
        if idx == 0:
            if (prev_job.start - computation_time) - ready_time > 0:
                logger.debug(f"Found an insertion slot before the first job {prev_job} on processor {proc}")
                job_start = ready_time
                min_schedule = ScheduleEvent(node, job_start, job_start+computation_time, proc, wf_id)
                break
        if idx == len(job_list)-1:
            job_start = max(ready_time, prev_job.end)
            min_schedule = ScheduleEvent(node, job_start, job_start + computation_time, proc, wf_id)
            break
        next_job = job_list[idx+1]
        #Start of next job - computation time == latest we can start in this window
        #Max(ready_time, previous job's end) == earliest we can start in this window
        #If there's space in there, schedule in it
        logger.debug(f"\tLooking to fit a job of length {computation_time} into a slot of size {next_job.start - max(ready_time, prev_job.end)}")
        if (next_job.start - computation_time) - max(ready_time, prev_job.end) >= 0:
            job_start = max(ready_time, prev_job.end)
            logger.debug(f"\tInsertion is feasible. Inserting job with start time {job_start} and end time {job_start + computation_time} into the time slot [{prev_job.end}, {next_job.start}]")
            min_schedule = ScheduleEvent(node, job_start, job_start + computation_time, proc, wf_id)
            break
    else:
        #For-else loop: the else executes if the for loop exits without break-ing, which in this case means the number of jobs on this processor are 0
        min_schedule = ScheduleEvent(node, ready_time, ready_time + computation_time, proc, wf_id)
    logger.debug(f"\tFor node {node} on processor {proc}, the EFT is {min_schedule}")
    return min_schedule    

def readCsvToNumpyMatrix(csv_file):
    """
    Given an input file consisting of a comma separated list of numeric values with a single header row and header column, 
    this function reads that data into a numpy matrix and strips the top row and leftmost column
    """
    with open(csv_file) as fd:
        logger.debug(f"Reading the contents of {csv_file} into a matrix")
        contents = fd.read()
        contentsList = contents.split('\n')
        contentsList = list(map(lambda line: line.split(','), contentsList))
        contentsList = list(filter(lambda arr: arr != [''], contentsList))
        
        matrix = np.array(contentsList)
        matrix = np.delete(matrix, 0, 0) # delete the first row (entry 0 along axis 0)
        matrix = np.delete(matrix, 0, 1) # delete the first column (entry 0 along axis 1)
        matrix = matrix.astype(float)
        logger.debug(f"After deleting the first row and column of input data, we are left with this matrix:\n{matrix}")
        return matrix

def readCsvToDict(csv_file):
    """
    Given an input file consisting of a comma separated list of numeric values with a single header row and header column, 
    this function reads that data into a dictionary with keys that are node numbers and values that are the CSV lists
    """
    with open(csv_file) as fd:
        matrix = readCsvToNumpyMatrix(csv_file)
        
        outputDict = {}
        for row_num, row in enumerate(matrix):
            outputDict[row_num] = row
        return outputDict

def readDagMatrix(dag_file, show_dag=False):
    """
    Given an input file consisting of a connectivity matrix, reads and parses it into a networkx Directional Graph (DiGraph)
    """
    matrix = readCsvToNumpyMatrix(dag_file)

    dag = nx.DiGraph(matrix)
    dag.remove_edges_from(
        # Remove all edges with weight of 0 since we have no placeholder for "this edge doesn't exist" in the input file
        [edge for edge in dag.edges() if dag.get_edge_data(*edge)['weight'] == '0.0']
    )

    if show_dag:
        nx.draw(dag, pos=nx.nx_pydot.graphviz_layout(dag, prog='dot'), with_labels=True)
        # nx.draw(dag, with_labels=True)
        plt.show()

    return dag

def generate_argparser():
    parser = argparse.ArgumentParser(description="A tool for finding HEFT schedules for given DAG task graphs")
    parser.add_argument("-d", "--dag_file", 
                        help="File containing input DAG to be scheduled. Uses default 10 node dag from Topcuoglu 2002 if none given.", 
                        type=str, default="test/canonicalgraph_task_connectivity_my_1.csv")
    parser.add_argument("-p", "--pe_connectivity_file", 
                        help="File containing connectivity/bandwidth information about PEs. Uses a default 3x3 matrix from Topcuoglu 2002 if none given. If communication startup costs (L) are needed, a \"Startup\" row can be used as the last CSV row", 
                        type=str, default="test/canonicalgraph_resource_BW_my_1.csv")
    parser.add_argument("-t", "--task_execution_file", 
                        help="File containing execution times of each task on each particular PE. Uses a default 10x3 matrix from Topcuoglu 2002 if none given.", 
                        type=str, default="test/canonicalgraph_task_exe_time_my_1.csv")
    parser.add_argument("-l", "--loglevel", 
                        help="The log level to be used in this module. Default: INFO", 
                        type=str, default="INFO", dest="loglevel", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    parser.add_argument("--metric",
                        help="Specify which metric to use when performing upward rank calculation",
                        type=RankMetric, default=RankMetric.MEAN, dest="rank_metric", choices=list(RankMetric))
    parser.add_argument("--showDAG", 
                        help="Switch used to enable display of the incoming task DAG", 
                        dest="showDAG", action="store_true")
    parser.add_argument("--showGantt", 
                        help="Switch used to enable display of the final scheduled Gantt chart", 
                        dest="showGantt", action="store_true")
    return parser

# 求平均计算时间
def get_average_computation(computation_matrix):
    total_sum = 0
    for row in computation_matrix:
        total_sum += sum(row)
    return total_sum / (computation_matrix.shape[0] * computation_matrix.shape[1])

# 求平均通信时间
def get_average_communication(dag):
    total_communication_cost = 0
    edges_num = len(dag.edges)
    for edge in dag.edges:
        total_communication_cost += dag[edge[0]][edge[1]]['weight']
    return total_communication_cost / edges_num


# 得到所有工作流的调度顺序
def get_all_nodes_sequence(sorted_workflows, sorted_nodes_arr):
    nodes_num = 0
    for item in sorted_nodes_arr:
        nodes_num += len(item)
    node_index = np.zeros_like(sorted_workflows)
    node_sequence = []
    while len(node_sequence) < nodes_num:
        for wf_id in sorted_workflows:
            if node_index[wf_id] >= len(sorted_nodes_arr[wf_id]):
                continue
            node_sequence.append(Node_sequence(wf_id, sorted_nodes_arr[wf_id][node_index[wf_id]]))
            node_index[wf_id] += 1
    return node_sequence


# 多工作流调度
def schedule_muti_workflows(workflows, sorted_workflows, communication_matrix, communication_startup, rank_metric=RankMetric.MEAN, **kwargs):
    _self = {
        'computation_matrix': workflows[0].computation_matrix,
        'dag': workflows[0].dag,
        'communication_matrix': communication_matrix,
        'communication_startup': communication_startup,
        'task_schedules': {},
        'proc_schedules': {},
        'numExistingJobs': 0,
        'time_offset': 0,
        'root_node': None
    }
    _self = SimpleNamespace(**_self)

    for i in range(_self.numExistingJobs + len(_self.computation_matrix)):
        _self.task_schedules[i] = None
    for i in range(len(_self.communication_matrix)):
        if i not in _self.proc_schedules:
            _self.proc_schedules[i] = []
    
    sorted_nodes_arr = []
    for iter in workflows:
        workflow = workflows[iter]
        _self.computation_matrix = workflow.computation_matrix
        # Nodes with no successors cause the any expression to be empty  
        # 找到entry node  
        root_node = [node for node in workflow.dag.nodes() if not any(True for _ in workflow.dag.predecessors(node))] # 没有任何前驱节点
        assert len(root_node) == 1, f"Expected a single root node, found {len(root_node)}"
        root_node = root_node[0]
        _self.root_node = root_node
        # 计算每个DAG中子任务的rankU
        _compute_ranku(_self, workflow.dag, metric=rank_metric, **kwargs)
        weight = workflow.dag.nodes()[0]['ranku']
        #为每个DAG内部的子任务排序
        sorted_nodes = sorted(workflow.dag.nodes(), key=lambda node: workflow.dag.nodes()[node]['ranku'], reverse=True)
        # if sorted_nodes[0] != root_node:
        #     logger.debug("Root node was not the first node in the sorted list. Must be a zero-cost and zero-weight placeholder node. Rearranging it so it is scheduled first\n")
        #     idx = sorted_nodes.index(root_node)
        #     sorted_nodes[idx], sorted_nodes[0] = sorted_nodes[0], sorted_nodes[idx]
        sorted_nodes_arr.append(sorted_nodes)

    # 如果是按照ranku进行排序的话，需要在此得到sorted_workflows数组
    if strategies == Strategies.L_RANK:
        key = lambda iter: workflows[iter].dag.nodes()[0]['ranku']
        reverse = True
        sorted_workflows = sorted(workflows, key = key, reverse = reverse)
    elif strategies == Strategies.S_RANK:
        key = lambda iter: workflows[iter].dag.nodes()[0]['ranku']
        reverse = False
        sorted_workflows = sorted(workflows, key = key, reverse = reverse)
    
    sequence = get_all_nodes_sequence(sorted_workflows, sorted_nodes_arr)
    
    # 按照顺序为每个node分配处理器
    for pair in sequence:
        node = pair.node_id
        wf_id = pair.wf_id

        _self.computation_matrix = workflows[wf_id].computation_matrix
        _self.dag = workflows[wf_id].dag

        # if _self.task_schedules[node] is not None:
        #     continue
        minTaskSchedule = ScheduleEvent(node, inf, inf, -1, 0)
        minEDP = inf
        # op_mode = kwargs.get("op_mode", OpMode.EFT)

        for proc in range(len(_self.communication_matrix)):
            taskschedule = _compute_eft(_self, _self.dag, node, proc, wf_id)
            if (taskschedule.end < minTaskSchedule.end):
                minTaskSchedule = taskschedule
        
        # 为节点分配最优处理器
        _self.task_schedules[node] = minTaskSchedule
        # 相应处理器的相应时间段被占用
        _self.proc_schedules[minTaskSchedule.proc].append(minTaskSchedule)
        # 已经被使用的时间段排序
        _self.proc_schedules[minTaskSchedule.proc] = sorted(_self.proc_schedules[minTaskSchedule.proc], key=lambda schedule_event: (schedule_event.end, schedule_event.start))

        # 保证相同处理器的前一个任务结束时间大于下一个任务的开始时间
        for proc in range(len(_self.proc_schedules)):
            for job in range(len(_self.proc_schedules[proc])-1):
                first_job = _self.proc_schedules[proc][job]
                second_job = _self.proc_schedules[proc][job+1]
                assert first_job.end <= second_job.start, \
                f"Jobs on a particular processor must finish before the next can begin, but job {first_job.task} on processor {first_job.proc} ends at {first_job.end} and its successor {second_job.task} starts at {second_job.start}"

    return _self.proc_schedules, _self.task_schedules

if __name__ == "__main__":
    workflow_num = 50
    argparser = generate_argparser()
    args = argparser.parse_args()
    
    # 日志器设置
    logger.setLevel(logging.getLevelName(args.loglevel))
    consolehandler = logging.StreamHandler()
    consolehandler.setLevel(logging.getLevelName(args.loglevel))
    consolehandler.setFormatter(logging.Formatter("%(levelname)8s : %(name)16s : %(message)s"))
    logger.addHandler(consolehandler)

    communication_matrix_path = "test/resource_BW.csv"
    computation_matrix_path = "test/task_exe_time_"
    dag_path = "test/task_connectivity_"
    # 读取处理器连通矩阵
    communication_matrix = readCsvToNumpyMatrix(communication_matrix_path)
    # 处理器启动开销，设置为0
    if (communication_matrix.shape[0] != communication_matrix.shape[1]):
        assert communication_matrix.shape[0]-1 == communication_matrix.shape[1], "If the communication_matrix CSV is non-square, there must only be a single additional row specifying the communication startup costs of each PE"
        logger.debug("Non-square communication matrix parsed. Stripping off the last row as communication startup costs")
        communication_startup = communication_matrix[-1, :]
        communication_matrix = communication_matrix[0:-1, :]
    else: 
        communication_startup = np.zeros(communication_matrix.shape[0])

    computation_matrix = []
    dag = []
    processor_schedules = None
    workflows = {}
    for i in range(0, workflow_num):
        computation_matrix_path_temp = computation_matrix_path + str(i) + ".csv"
        dag_path_temp = dag_path + str(i) + ".csv"
        # 任务处理时间矩阵
        # computation_matrix = readCsvToNumpyMatrix(args.task_execution_file)
        computation_matrix_temp = readCsvToNumpyMatrix(computation_matrix_path_temp)
        # 读取DAG图
        # dag = readDagMatrix(args.dag_file, args.showDAG) 
        dag_temp = readDagMatrix(dag_path_temp, args.showDAG)
        
        # 求平均计算时间
        average_computation = get_average_computation(computation_matrix_temp)
        # 求平均通信时间
        average_communication = get_average_communication(dag_temp)

        # 添加一列，表示任务编号
        tasks_num = computation_matrix_temp.shape[0]
        new_col = []
        for c in range(0, tasks_num):
            new_col.append(float(str(int(i)) + "." + str(int(c))))
        # computation_matrix_temp = np.insert(computation_matrix_temp, computation_matrix_temp.shape[1], values = new_col, axis = 1)

        workflows_cur = Workflows(dag_temp, computation_matrix_temp, average_computation, average_communication, i)
        
        workflows[workflows_cur.id] = workflows_cur

        # computation_matrix.append(computation_matrix_temp)
        # dag.append(dag_temp)
    strategies_list = [
        Strategies.S_COMP,
        Strategies.L_COMP,
        Strategies.S_COMM,
        Strategies.L_COMM,
        Strategies.S_RANK,
        Strategies.L_RANK,
        Strategies.S_SQUE,
        Strategies.L_SQUE
    ]
    for strategies in strategies_list:
        if strategies == Strategies.L_COMP:
            key = lambda iter: workflows[iter].comp
            reverse = True
            
        elif strategies == Strategies.S_COMP:
            key = lambda iter: workflows[iter].comp
            reverse = False

        elif strategies == Strategies.L_COMM:
            key = lambda iter: workflows[iter].comm
            reverse = True

        elif strategies == Strategies.S_COMM:
            key = lambda iter: workflows[iter].comm
            reverse = False

        elif strategies == Strategies.L_SQUE:
            key = lambda iter: workflows[iter].id
            reverse = True

        elif strategies == Strategies.S_SQUE:
            key = lambda iter: workflows[iter].id
            reverse = False

        # 求得工作流的调度顺序
        sorted_workflows = sorted(workflows, key = key, reverse=reverse)
        processor_schedules, _ = schedule_muti_workflows(workflows = workflows, sorted_workflows = sorted_workflows, communication_matrix = communication_matrix, communication_startup=communication_startup)
    
        # 开始调度
        # processor_schedules, _, _ = schedule_dag(dag, communication_matrix=communication_matrix, communication_startup=communication_startup, proc_schedules=processor_schedules, computation_matrix=computation_matrix, rank_metric=args.rank_metric)
        total_endtime = 0
        for proc, jobs in processor_schedules.items():
            # logger.info(f"Processor {proc} has the following jobs:")
            # logger.info(f"\t{jobs}")
            if total_endtime < jobs[-1].end:
                total_endtime = jobs[-1].end
        if args.showGantt:
            showGanttChart(processor_schedules)
        logger.info(f"\n************************{strategies}  The end time is:{total_endtime}**********************\n")