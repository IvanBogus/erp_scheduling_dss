"""
Lab work 3. Project practicum.

ERP decision support prototype for job-shop scheduling with two approaches:
1. Classic CP-SAT optimization with Google OR-Tools.
2. R&D multicriteria dispatching model that reduces the amount of search.

The script generates Gantt charts, verifies schedules, and compares the
calculation complexity as the number of input operations grows.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from ortools.sat.python import cp_model


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIGURES_DIR = PROJECT_ROOT / "reports" / "figures"
TABLES_DIR = PROJECT_ROOT / "reports" / "tables"


@dataclass(frozen=True)
class Operation:
    job_id: int
    task_id: int
    machine_id: int
    duration: int
    due_date: int
    priority: int


@dataclass(frozen=True)
class ScheduledOperation:
    job_id: int
    task_id: int
    machine_id: int
    start: int
    end: int
    duration: int
    due_date: int
    priority: int


@dataclass(frozen=True)
class ScheduleResult:
    name: str
    schedule: list[ScheduledOperation]
    makespan: int
    total_tardiness: int
    complexity_units: int
    wall_time: float
    solver_status: str = ""
    conflicts: int = 0
    branches: int = 0


def generate_jobs(job_count: int, machine_count: int, seed: int = 42) -> list[list[Operation]]:
    """Generate deterministic ERP production orders for a job-shop problem."""
    rng = np.random.default_rng(seed + job_count * 17 + machine_count * 31)
    jobs: list[list[Operation]] = []

    for job_id in range(job_count):
        route = list(rng.permutation(machine_count))
        task_count = machine_count
        raw_durations = rng.integers(2, 12, size=task_count)
        due_date = int(raw_durations.sum() * rng.uniform(1.25, 1.85))
        priority = int(rng.integers(1, 6))

        job: list[Operation] = []
        for task_id, machine_id in enumerate(route):
            job.append(
                Operation(
                    job_id=job_id,
                    task_id=task_id,
                    machine_id=int(machine_id),
                    duration=int(raw_durations[task_id]),
                    due_date=due_date,
                    priority=priority,
                )
            )
        jobs.append(job)

    return jobs


def flatten_jobs(jobs: list[list[Operation]]) -> list[Operation]:
    return [operation for job in jobs for operation in job]


def horizon_for(jobs: list[list[Operation]]) -> int:
    return sum(operation.duration for operation in flatten_jobs(jobs))


def estimate_cp_sat_ordering_complexity(jobs: list[list[Operation]]) -> int:
    """
    Estimate the combinatorial CP-SAT ordering space.

    Each machine receives several operations. In the worst case CP-SAT has to
    reason about the possible mutual orders of operations on every machine.
    """
    operations_by_machine: dict[int, int] = {}
    for operation in flatten_jobs(jobs):
        operations_by_machine[operation.machine_id] = operations_by_machine.get(operation.machine_id, 0) + 1

    complexity = 1
    for operation_count in operations_by_machine.values():
        complexity *= math.factorial(operation_count)
    return complexity


def solve_with_cp_sat(
    jobs: list[list[Operation]],
    time_limit_seconds: float = 10.0,
    makespan_upper_bound: int | None = None,
    name: str = "CP-SAT baseline",
    use_search_effort_complexity: bool = False,
    preprocessing_complexity: int = 0,
) -> ScheduleResult:
    """Build and solve the classic CP-SAT job-shop model."""
    start_time = time.perf_counter()
    model = cp_model.CpModel()
    horizon = horizon_for(jobs)
    machine_count = 1 + max(operation.machine_id for operation in flatten_jobs(jobs))

    task_vars: dict[tuple[int, int], tuple[cp_model.IntVar, cp_model.IntVar, cp_model.IntervalVar]] = {}
    intervals_by_machine: dict[int, list[cp_model.IntervalVar]] = {machine: [] for machine in range(machine_count)}

    for job in jobs:
        for operation in job:
            suffix = f"_{operation.job_id}_{operation.task_id}"
            start = model.NewIntVar(0, horizon, f"start{suffix}")
            end = model.NewIntVar(0, horizon, f"end{suffix}")
            interval = model.NewIntervalVar(start, operation.duration, end, f"interval{suffix}")
            task_vars[(operation.job_id, operation.task_id)] = (start, end, interval)
            intervals_by_machine[operation.machine_id].append(interval)

    for intervals in intervals_by_machine.values():
        model.AddNoOverlap(intervals)

    for job in jobs:
        for left, right in zip(job, job[1:]):
            model.Add(
                task_vars[(right.job_id, right.task_id)][0]
                >= task_vars[(left.job_id, left.task_id)][1]
            )

    last_task_ends = [task_vars[(job[-1].job_id, job[-1].task_id)][1] for job in jobs]
    makespan_var = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan_var, last_task_ends)
    if makespan_upper_bound is not None:
        model.Add(makespan_var <= makespan_upper_bound)

    tardiness_vars = []
    for job in jobs:
        last_operation = job[-1]
        tardiness = model.NewIntVar(0, horizon, f"tardiness_{last_operation.job_id}")
        model.Add(tardiness >= task_vars[(last_operation.job_id, last_operation.task_id)][1] - last_operation.due_date)
        tardiness_vars.append(tardiness)

    model.Minimize(makespan_var)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(model)
    wall_time = time.perf_counter() - start_time

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT did not find a feasible schedule. Status: {solver.StatusName(status)}")

    schedule = []
    for operation in flatten_jobs(jobs):
        start_var, end_var, _ = task_vars[(operation.job_id, operation.task_id)]
        schedule.append(
            ScheduledOperation(
                job_id=operation.job_id,
                task_id=operation.task_id,
                machine_id=operation.machine_id,
                start=int(solver.Value(start_var)),
                end=int(solver.Value(end_var)),
                duration=operation.duration,
                due_date=operation.due_date,
                priority=operation.priority,
            )
        )

    branches = int(solver.NumBranches())
    conflicts = int(solver.NumConflicts())
    if use_search_effort_complexity:
        complexity_units = preprocessing_complexity + max(1, branches + conflicts)
    else:
        complexity_units = estimate_cp_sat_ordering_complexity(jobs)

    return ScheduleResult(
        name=name,
        schedule=sorted(schedule, key=lambda item: (item.machine_id, item.start, item.job_id)),
        makespan=int(solver.Value(makespan_var)),
        total_tardiness=calculate_total_tardiness(schedule),
        complexity_units=complexity_units,
        wall_time=wall_time,
        solver_status=solver.StatusName(status),
        conflicts=conflicts,
        branches=branches,
    )


def normalize_min(value: float, values: list[float]) -> float:
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return 1.0
    return (high - value) / (high - low)


def normalize_max(value: float, values: list[float]) -> float:
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return 1.0
    return (value - low) / (high - low)


def solve_with_multicriteria_dispatching(jobs: list[list[Operation]]) -> ScheduleResult:
    """
    Build a schedule with multicriteria scoring of currently available operations.

    Criteria:
    - shortest processing time, min;
    - earliest due date/slack, min;
    - largest remaining work in the job, max;
    - highest business priority, max.
    """
    start_time = time.perf_counter()
    machine_count = 1 + max(operation.machine_id for operation in flatten_jobs(jobs))
    machine_available = [0 for _ in range(machine_count)]
    job_available = [0 for _ in jobs]
    next_task = [0 for _ in jobs]
    remaining_work = [
        [sum(operation.duration for operation in job[task_id:]) for task_id in range(len(job))]
        for job in jobs
    ]

    weights = {
        "earliest_start": 0.35,
        "duration": 0.25,
        "slack": 0.20,
        "remaining_work": 0.10,
        "priority": 0.10,
    }

    schedule: list[ScheduledOperation] = []
    complexity_units = 0

    while len(schedule) < len(flatten_jobs(jobs)):
        candidates = []
        for job_id, task_id in enumerate(next_task):
            if task_id < len(jobs[job_id]):
                operation = jobs[job_id][task_id]
                earliest_start = max(machine_available[operation.machine_id], job_available[job_id])
                slack = operation.due_date - earliest_start - remaining_work[job_id][task_id]
                candidates.append((operation, earliest_start, slack, remaining_work[job_id][task_id]))

        complexity_units += len(candidates) * len(weights)
        earliest_starts = [item[1] for item in candidates]
        durations = [item[0].duration for item in candidates]
        slacks = [item[2] for item in candidates]
        remaining = [item[3] for item in candidates]
        priorities = [item[0].priority for item in candidates]

        best_score = -1.0
        best_candidate = None
        for operation, earliest_start, slack, remaining_duration in candidates:
            score = (
                weights["earliest_start"] * normalize_min(earliest_start, earliest_starts)
                + weights["duration"] * normalize_min(operation.duration, durations)
                + weights["slack"] * normalize_min(slack, slacks)
                + weights["remaining_work"] * normalize_max(remaining_duration, remaining)
                + weights["priority"] * normalize_max(operation.priority, priorities)
            )
            if score > best_score:
                best_score = score
                best_candidate = (operation, earliest_start)

        if best_candidate is None:
            raise RuntimeError("No multicriteria candidate was selected.")

        operation, start = best_candidate
        end = start + operation.duration
        schedule.append(
            ScheduledOperation(
                job_id=operation.job_id,
                task_id=operation.task_id,
                machine_id=operation.machine_id,
                start=start,
                end=end,
                duration=operation.duration,
                due_date=operation.due_date,
                priority=operation.priority,
            )
        )
        machine_available[operation.machine_id] = end
        job_available[operation.job_id] = end
        next_task[operation.job_id] += 1

    wall_time = time.perf_counter() - start_time

    return ScheduleResult(
        name="R&D multicriteria",
        schedule=sorted(schedule, key=lambda item: (item.machine_id, item.start, item.job_id)),
        makespan=max(item.end for item in schedule),
        total_tardiness=calculate_total_tardiness(schedule),
        complexity_units=complexity_units,
        wall_time=wall_time,
    )


def calculate_total_tardiness(schedule: list[ScheduledOperation]) -> int:
    last_by_job: dict[int, ScheduledOperation] = {}
    for item in schedule:
        if item.job_id not in last_by_job or item.task_id > last_by_job[item.job_id].task_id:
            last_by_job[item.job_id] = item
    return sum(max(0, item.end - item.due_date) for item in last_by_job.values())


def verify_schedule(jobs: list[list[Operation]], schedule: list[ScheduledOperation]) -> None:
    expected = {(operation.job_id, operation.task_id): operation for operation in flatten_jobs(jobs)}
    actual = {(item.job_id, item.task_id): item for item in schedule}
    if set(expected) != set(actual):
        raise AssertionError("Schedule does not contain exactly all operations.")

    for key, operation in expected.items():
        scheduled = actual[key]
        if scheduled.duration != operation.duration:
            raise AssertionError(f"Wrong duration for operation {key}.")
        if scheduled.end - scheduled.start != operation.duration:
            raise AssertionError(f"Wrong interval length for operation {key}.")

    for job in jobs:
        for left, right in zip(job, job[1:]):
            if actual[(right.job_id, right.task_id)].start < actual[(left.job_id, left.task_id)].end:
                raise AssertionError(f"Precedence violation in job {right.job_id}.")

    by_machine: dict[int, list[ScheduledOperation]] = {}
    for item in schedule:
        by_machine.setdefault(item.machine_id, []).append(item)

    for machine_id, items in by_machine.items():
        ordered = sorted(items, key=lambda item: item.start)
        for left, right in zip(ordered, ordered[1:]):
            if right.start < left.end:
                raise AssertionError(f"Machine overlap on machine {machine_id}.")


def plot_gantt(result: ScheduleResult, output_path: Path) -> None:
    machines = sorted({item.machine_id for item in result.schedule})
    fig_height = max(4, 0.7 * len(machines) + 2)
    fig, ax = plt.subplots(figsize=(12, fig_height))
    colors = plt.cm.tab20(np.linspace(0, 1, max(1, len({item.job_id for item in result.schedule}))))

    for item in result.schedule:
        ax.barh(
            y=item.machine_id,
            width=item.duration,
            left=item.start,
            height=0.55,
            color=colors[item.job_id % len(colors)],
            edgecolor="black",
            linewidth=0.8,
        )
        ax.text(
            item.start + item.duration / 2,
            item.machine_id,
            f"J{item.job_id}.{item.task_id}",
            ha="center",
            va="center",
            fontsize=9,
            color="black",
        )

    ax.set_yticks(machines)
    ax.set_yticklabels([f"Machine {machine}" for machine in machines])
    ax.set_xlabel("Time units")
    ax.set_title(f"{result.name}: Gantt chart, makespan={result.makespan}, tardiness={result.total_tardiness}")
    ax.grid(axis="x", linestyle="--", alpha=0.35)
    ax.invert_yaxis()
    fig.tight_layout()
    save_figure(fig, output_path)
    plt.close(fig)


def write_schedule_csv(result: ScheduleResult, output_path: Path) -> None:
    rows = [
        {
            "method": result.name,
            "job": item.job_id,
            "task": item.task_id,
            "machine": item.machine_id,
            "start": item.start,
            "end": item.end,
            "duration": item.duration,
            "due_date": item.due_date,
            "priority": item.priority,
        }
        for item in result.schedule
    ]
    pd.DataFrame(rows).to_csv(writable_output_path(output_path), index=False, encoding="utf-8")


def compare_complexity(machine_count: int, max_jobs: int, time_limit_seconds: float) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for job_count in range(3, max_jobs + 1):
        jobs = generate_jobs(job_count=job_count, machine_count=machine_count)
        cp_sat = solve_with_cp_sat(jobs, time_limit_seconds=time_limit_seconds)
        multicriteria = solve_with_multicriteria_dispatching(jobs)
        hybrid = solve_with_cp_sat(
            jobs,
            time_limit_seconds=time_limit_seconds,
            makespan_upper_bound=multicriteria.makespan,
            name="Hybrid CP-SAT + R&D",
            use_search_effort_complexity=True,
            preprocessing_complexity=multicriteria.complexity_units,
        )
        verify_schedule(jobs, cp_sat.schedule)
        verify_schedule(jobs, multicriteria.schedule)
        verify_schedule(jobs, hybrid.schedule)
        rows.append(
            {
                "jobs": job_count,
                "operations": job_count * machine_count,
                "cp_sat_complexity": cp_sat.complexity_units,
                "multicriteria_complexity": multicriteria.complexity_units,
                "hybrid_complexity": hybrid.complexity_units,
                "cp_sat_time": cp_sat.wall_time,
                "multicriteria_time": multicriteria.wall_time,
                "hybrid_time": hybrid.wall_time,
                "cp_sat_makespan": cp_sat.makespan,
                "multicriteria_makespan": multicriteria.makespan,
                "hybrid_makespan": hybrid.makespan,
                "cp_sat_tardiness": cp_sat.total_tardiness,
                "multicriteria_tardiness": multicriteria.total_tardiness,
                "hybrid_tardiness": hybrid.total_tardiness,
                "cp_sat_branches": cp_sat.branches,
                "cp_sat_conflicts": cp_sat.conflicts,
                "hybrid_branches": hybrid.branches,
                "hybrid_conflicts": hybrid.conflicts,
            }
        )
    return rows


def write_complexity_csv(rows: list[dict[str, float]], output_path: Path) -> None:
    pd.DataFrame(rows).to_csv(writable_output_path(output_path), index=False, encoding="utf-8")


def plot_complexity(rows: list[dict[str, float]], output_path: Path) -> None:
    operations = [row["operations"] for row in rows]
    cp_complexity = [max(1, row["cp_sat_complexity"]) for row in rows]
    rd_complexity = [max(1, row["multicriteria_complexity"]) for row in rows]
    hybrid_complexity = [max(1, row["hybrid_complexity"]) for row in rows]

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(operations, cp_complexity, marker="o", linewidth=2.2, label="CP-SAT: possible machine orderings")
    ax.plot(operations, rd_complexity, marker="s", linewidth=2.2, label="R&D multicriteria: score evaluations")
    ax.plot(operations, hybrid_complexity, marker="^", linewidth=2.2, label="Hybrid: R&D + bounded CP-SAT search")
    ax.set_yscale("log")
    ax.set_xlabel("Number of input operations")
    ax.set_ylabel("Complexity units, log scale")
    ax.set_title("Calculation complexity growth")
    ax.grid(True, which="both", linestyle="--", alpha=0.35)
    ax.legend()
    fig.tight_layout()
    save_figure(fig, output_path)
    plt.close(fig)


def plot_architecture(output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.axis("off")

    boxes = [
        ("Input data\nERP orders, machines,\ndurations, due dates", (0.08, 0.70)),
        ("CP-SAT model\ninterval variables,\nNoOverlap, precedence", (0.38, 0.82)),
        ("R&D multicriteria model\nlocal scoring of\navailable operations", (0.38, 0.55)),
        ("Hybrid model\nR&D upper bound\nfor CP-SAT", (0.62, 0.68)),
        ("Verification\nprecedence,\nno overlap, completeness", (0.82, 0.70)),
        ("Results\nGantt charts,\nCSV tables,\ncomplexity graph", (0.82, 0.38)),
    ]

    for text, (x, y) in boxes:
        ax.text(
            x,
            y,
            text,
            ha="center",
            va="center",
            fontsize=12,
            bbox={
                "boxstyle": "round,pad=0.45",
                "facecolor": "#f7f7f7",
                "edgecolor": "#333333",
                "linewidth": 1.2,
            },
        )

    arrows = [
        ((0.20, 0.72), (0.30, 0.82)),
        ((0.20, 0.68), (0.30, 0.55)),
        ((0.50, 0.82), (0.56, 0.72)),
        ((0.50, 0.55), (0.56, 0.65)),
        ((0.68, 0.68), (0.74, 0.70)),
        ((0.50, 0.82), (0.74, 0.72)),
        ((0.50, 0.55), (0.74, 0.68)),
        ((0.82, 0.62), (0.82, 0.47)),
    ]

    for start, end in arrows:
        ax.annotate("", xy=end, xytext=start, arrowprops={"arrowstyle": "->", "linewidth": 1.8})

    ax.set_title("ERP/DSS scheduling practicum architecture", fontsize=16, pad=18)
    fig.tight_layout()
    save_figure(fig, output_path)
    plt.close(fig)


def save_figure(fig: plt.Figure, output_path: Path) -> Path:
    """Save a figure and create a numbered copy if the target is locked."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.stem}_tmp{output_path.suffix}")
    fig.savefig(temporary_path, dpi=180)

    try:
        os.replace(temporary_path, output_path)
        return output_path
    except PermissionError:
        fallback_path = next_available_latest_path(output_path)
        os.replace(temporary_path, fallback_path)
        print(f"Could not overwrite locked file: {output_path}")
        print(f"Saved updated figure as: {fallback_path}")
        return fallback_path


def writable_output_path(output_path: Path) -> Path:
    """Return the target path, or a numbered copy if Windows has locked it."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("a", encoding="utf-8"):
            pass
        return output_path
    except PermissionError:
        fallback_path = next_available_latest_path(output_path)
        print(f"Could not overwrite locked file: {output_path}")
        print(f"Saved updated table as: {fallback_path}")
        return fallback_path


def next_available_latest_path(output_path: Path) -> Path:
    """Return name_latest_1.ext, name_latest_2.ext, ... without overwriting."""
    index = 1
    while True:
        candidate = output_path.with_name(f"{output_path.stem}_latest_{index}{output_path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def run(args: argparse.Namespace) -> None:
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    TABLES_DIR.mkdir(parents=True, exist_ok=True)

    main_jobs = generate_jobs(job_count=args.jobs, machine_count=args.machines, seed=args.seed)
    cp_sat = solve_with_cp_sat(main_jobs, time_limit_seconds=args.time_limit)
    multicriteria = solve_with_multicriteria_dispatching(main_jobs)
    hybrid = solve_with_cp_sat(
        main_jobs,
        time_limit_seconds=args.time_limit,
        makespan_upper_bound=multicriteria.makespan,
        name="Hybrid CP-SAT + R&D",
        use_search_effort_complexity=True,
        preprocessing_complexity=multicriteria.complexity_units,
    )

    verify_schedule(main_jobs, cp_sat.schedule)
    verify_schedule(main_jobs, multicriteria.schedule)
    verify_schedule(main_jobs, hybrid.schedule)

    plot_gantt(cp_sat, FIGURES_DIR / "gantt_cp_sat.png")
    plot_gantt(multicriteria, FIGURES_DIR / "gantt_multicriteria.png")
    plot_gantt(hybrid, FIGURES_DIR / "gantt_hybrid.png")
    plot_architecture(FIGURES_DIR / "architecture.png")
    write_schedule_csv(cp_sat, TABLES_DIR / "schedule_cp_sat.csv")
    write_schedule_csv(multicriteria, TABLES_DIR / "schedule_multicriteria.csv")
    write_schedule_csv(hybrid, TABLES_DIR / "schedule_hybrid.csv")

    complexity_rows = compare_complexity(
        machine_count=args.machines,
        max_jobs=args.max_complexity_jobs,
        time_limit_seconds=args.time_limit,
    )
    write_complexity_csv(complexity_rows, TABLES_DIR / "complexity_results.csv")
    plot_complexity(complexity_rows, FIGURES_DIR / "complexity_comparison.png")

    print("Lab work 3 project practicum completed.")
    print(f"CP-SAT: makespan={cp_sat.makespan}, tardiness={cp_sat.total_tardiness}, "
          f"complexity={cp_sat.complexity_units}, status={cp_sat.solver_status}")
    print(f"R&D multicriteria: makespan={multicriteria.makespan}, "
          f"tardiness={multicriteria.total_tardiness}, complexity={multicriteria.complexity_units}")
    print(f"Hybrid CP-SAT + R&D: makespan={hybrid.makespan}, tardiness={hybrid.total_tardiness}, "
          f"complexity={hybrid.complexity_units}, status={hybrid.solver_status}")
    print(f"Figures directory: {FIGURES_DIR}")
    print(f"Tables directory: {TABLES_DIR}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lab work 3 project practicum runner.")
    parser.add_argument("--jobs", type=int, default=8, help="Number of ERP production orders.")
    parser.add_argument("--machines", type=int, default=5, help="Number of machines/resources.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic data generation.")
    parser.add_argument("--time-limit", type=float, default=5.0, help="CP-SAT time limit per run.")
    parser.add_argument("--max-complexity-jobs", type=int, default=10, help="Largest job count for complexity chart.")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
