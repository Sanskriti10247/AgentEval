import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agenteval_core.evaluation.taxonomy import MAX_EVALUATION_SPEC_BYTES, Difficulty
from agenteval_core.models.base import Base, CreatedAt, NullableJSONB, UUIDPk, sql_values


class Task(UUIDPk, CreatedAt, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("task_type IN ('command','agent')", name="task_type"),
        # Agent runs need minutes; command tasks keep the S1 limit.
        CheckConstraint(
            "timeout_s >= 1 AND timeout_s <= CASE task_type WHEN 'agent' THEN 1800 ELSE 300 END",
            name="timeout_s",
        ),
        CheckConstraint("(task_type = 'command') = (command IS NOT NULL)", name="command"),
        CheckConstraint("task_type <> 'agent' OR instructions IS NOT NULL", name="instructions"),
        CheckConstraint("task_type = 'agent' OR test_command IS NULL", name="test_command"),
        # Phase 4 grades agent tasks only (D4): command tasks stay as in S1.
        CheckConstraint("task_type = 'agent' OR evaluation IS NULL", name="evaluation"),
        CheckConstraint("task_type = 'agent' OR dataset_id IS NULL", name="dataset_id"),
        CheckConstraint(
            f"octet_length(evaluation::text) <= {MAX_EVALUATION_SPEC_BYTES}",
            name="evaluation_size",
        ),
        CheckConstraint(f"difficulty IN ({sql_values(Difficulty)})", name="difficulty"),
        CheckConstraint("memory_mb BETWEEN 64 AND 1024", name="memory_mb"),
        CheckConstraint("cpu_limit >= 0.1 AND cpu_limit <= 2.0", name="cpu_limit"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="command")
    image: Mapped[str] = mapped_column(String(255), nullable=False)
    # command tasks: the argv to run. agent tasks: NULL (the agent decides).
    command: Mapped[list[str] | None] = mapped_column(NullableJSONB)
    # agent tasks: the prompt given to the agent.
    instructions: Mapped[str | None] = mapped_column(Text)
    # agent tasks: argv behind the run_tests tool (the model can't choose it).
    test_command: Mapped[list[str] | None] = mapped_column(NullableJSONB)
    files: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_limit: Mapped[float] = mapped_column(Float, nullable=False)
    # Phase 4. dataset membership is fixed at creation (tasks are immutable).
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("datasets.id"))
    difficulty: Mapped[str | None] = mapped_column(String(16))
    # INTERNAL evaluator data: checks, expected values and hidden files (D11).
    # Never returned by the API or shown to the model; only mounted into the
    # isolated evaluation container's /eval.
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(NullableJSONB)
