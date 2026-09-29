from datetime import datetime
from sqlalchemy import (
    Column,
    JSON,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    repository = Column(String(500))
    status = Column(String(50), nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship(
        "Task",
        back_populates="project",
        cascade="all, delete-orphan"
    )


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    provider = Column(String(100), nullable=False)
    model = Column(String(255))
    status = Column(String(50), nullable=False, default="offline")
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("Task", back_populates="agent")
    configuration = relationship(
        "AgentConfiguration", back_populates="agent", uselist=False,
        cascade="all, delete-orphan", lazy="selectin",
    )


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False
    )
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True
    )

    title = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(50), nullable=False, default="pending")
    priority = Column(Integer, nullable=False, default=50)

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    project = relationship("Project", back_populates="tasks")
    agent = relationship("Agent", back_populates="tasks")


class AgentConfiguration(Base):
    """Additive table: existing agents/tasks need no ALTER TABLE or backfill."""
    __tablename__ = "agent_configurations"

    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    role = Column(String(255), nullable=False, default="assistant")
    capabilities = Column(JSON, nullable=False, default=list)
    permissions = Column(JSON, nullable=False, default=dict)

    agent = relationship("Agent", back_populates="configuration")
