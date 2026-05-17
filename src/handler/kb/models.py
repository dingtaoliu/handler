"""Data models for email database objects."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Message:
    """Represents an email message in the database."""

    id: Optional[int] = None
    gmail_id: str = ""
    thread_id: Optional[str] = None
    subject: Optional[str] = None
    from_email: str = ""
    from_name: Optional[str] = None
    to_emails: Optional[List[str]] = None
    cc_emails: Optional[List[str]] = None
    date: Optional[str] = None
    date_timestamp: Optional[int] = None
    body_plain: Optional[str] = None
    body_html: Optional[str] = None
    labels: Optional[List[str]] = None
    file_path: Optional[str] = None
    has_attachments: bool = False
    attachment_count: int = 0
    attachment_info: Optional[List[dict]] = None
    size_bytes: Optional[int] = None
    indexed_at: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Create a Message from a dictionary (e.g., from database row).

        Args:
            data: Dictionary containing message fields

        Returns:
            Message instance
        """
        import json

        # Parse JSON fields if they're strings
        to_emails = data.get("to_emails")
        if isinstance(to_emails, str):
            to_emails = json.loads(to_emails) if to_emails else None

        cc_emails = data.get("cc_emails")
        if isinstance(cc_emails, str):
            cc_emails = json.loads(cc_emails) if cc_emails else None

        labels = data.get("labels")
        if isinstance(labels, str):
            labels = json.loads(labels) if labels else None

        attachment_info = data.get("attachment_info")
        if isinstance(attachment_info, str):
            attachment_info = json.loads(attachment_info) if attachment_info else None

        return cls(
            id=data.get("id"),
            gmail_id=data.get("gmail_id", ""),
            thread_id=data.get("thread_id"),
            subject=data.get("subject"),
            from_email=data.get("from_email", ""),
            from_name=data.get("from_name"),
            to_emails=to_emails,
            cc_emails=cc_emails,
            date=data.get("date"),
            date_timestamp=data.get("date_timestamp"),
            body_plain=data.get("body_plain"),
            body_html=data.get("body_html"),
            labels=labels,
            file_path=data.get("file_path"),
            has_attachments=bool(data.get("has_attachments", False)),
            attachment_count=data.get("attachment_count", 0),
            attachment_info=attachment_info,
            size_bytes=data.get("size_bytes"),
            indexed_at=data.get("indexed_at"),
        )

    def to_dict(self) -> dict:
        """Convert Message to a dictionary for database insertion.

        Returns:
            Dictionary with all message fields
        """
        import json

        data = {}

        # Only include id if it's set
        if self.id is not None:
            data["id"] = self.id

        data["gmail_id"] = self.gmail_id

        if self.thread_id is not None:
            data["thread_id"] = self.thread_id
        if self.subject is not None:
            data["subject"] = self.subject

        data["from_email"] = self.from_email

        if self.from_name is not None:
            data["from_name"] = self.from_name
        if self.to_emails is not None:
            data["to_emails"] = json.dumps(self.to_emails)
        if self.cc_emails is not None:
            data["cc_emails"] = json.dumps(self.cc_emails)
        if self.date is not None:
            data["date"] = self.date
        if self.date_timestamp is not None:
            data["date_timestamp"] = self.date_timestamp
        if self.body_plain is not None:
            data["body_plain"] = self.body_plain
        if self.body_html is not None:
            data["body_html"] = self.body_html
        if self.labels is not None:
            data["labels"] = json.dumps(self.labels)
        if self.file_path is not None:
            data["file_path"] = self.file_path

        data["has_attachments"] = self.has_attachments
        data["attachment_count"] = self.attachment_count

        if self.attachment_info is not None:
            data["attachment_info"] = json.dumps(self.attachment_info)
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        if self.indexed_at is not None:
            data["indexed_at"] = self.indexed_at

        return data


@dataclass
class Category:
    """Represents an email category."""

    id: Optional[int] = None
    name: str = ""
    parent_id: Optional[int] = None
    description: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    automation_enabled: bool = False
    created_at: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Category":
        """Create a Category from a dictionary."""
        return cls(
            id=data.get("id"),
            name=data.get("name", ""),
            parent_id=data.get("parent_id"),
            description=data.get("description"),
            color=data.get("color"),
            icon=data.get("icon"),
            automation_enabled=bool(data.get("automation_enabled", False)),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict:
        """Convert Category to a dictionary."""
        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["name"] = self.name
        if self.parent_id is not None:
            data["parent_id"] = self.parent_id
        if self.description is not None:
            data["description"] = self.description
        if self.color is not None:
            data["color"] = self.color
        if self.icon is not None:
            data["icon"] = self.icon
        data["automation_enabled"] = self.automation_enabled
        if self.created_at is not None:
            data["created_at"] = self.created_at
        return data


@dataclass
class EmailCategory:
    """Represents a categorization result for an email."""

    id: Optional[int] = None
    message_id: int = 0
    category_id: int = 0
    confidence: float = 0.0
    method: Optional[str] = None
    model_name: Optional[str] = None
    sub_category: Optional[str] = None
    tags: Optional[List[str]] = None
    suggested_actions: Optional[List[str]] = None
    categorized_at: Optional[int] = None
    reviewed: bool = False
    corrected_category_id: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> "EmailCategory":
        """Create an EmailCategory from a dictionary."""
        import json

        tags = data.get("tags")
        if isinstance(tags, str):
            tags = json.loads(tags) if tags else None

        suggested_actions = data.get("suggested_actions")
        if isinstance(suggested_actions, str):
            suggested_actions = (
                json.loads(suggested_actions) if suggested_actions else None
            )

        return cls(
            id=data.get("id"),
            message_id=data.get("message_id", 0),
            category_id=data.get("category_id", 0),
            confidence=data.get("confidence", 0.0),
            method=data.get("method"),
            model_name=data.get("model_name"),
            sub_category=data.get("sub_category"),
            tags=tags,
            suggested_actions=suggested_actions,
            categorized_at=data.get("categorized_at"),
            reviewed=bool(data.get("reviewed", False)),
            corrected_category_id=data.get("corrected_category_id"),
        )

    def to_dict(self) -> dict:
        """Convert EmailCategory to a dictionary."""
        import json

        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["message_id"] = self.message_id
        data["category_id"] = self.category_id
        data["confidence"] = self.confidence
        if self.method is not None:
            data["method"] = self.method
        if self.model_name is not None:
            data["model_name"] = self.model_name
        if self.sub_category is not None:
            data["sub_category"] = self.sub_category
        if self.tags is not None:
            data["tags"] = json.dumps(self.tags)
        if self.suggested_actions is not None:
            data["suggested_actions"] = json.dumps(self.suggested_actions)
        if self.categorized_at is not None:
            data["categorized_at"] = self.categorized_at
        data["reviewed"] = self.reviewed
        if self.corrected_category_id is not None:
            data["corrected_category_id"] = self.corrected_category_id
        return data


@dataclass
class IndexingProgress:
    """Represents indexing progress for a year/month."""

    id: Optional[int] = None
    year: int = 0
    month: Optional[int] = None
    last_message_id: Optional[str] = None
    total_messages: int = 0
    indexed_messages: int = 0
    started_at: Optional[int] = None
    updated_at: Optional[int] = None
    completed: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "IndexingProgress":
        """Create an IndexingProgress from a dictionary."""
        return cls(
            id=data.get("id"),
            year=data.get("year", 0),
            month=data.get("month"),
            last_message_id=data.get("last_message_id"),
            total_messages=data.get("total_messages", 0),
            indexed_messages=data.get("indexed_messages", 0),
            started_at=data.get("started_at"),
            updated_at=data.get("updated_at"),
            completed=bool(data.get("completed", False)),
        )

    def to_dict(self) -> dict:
        """Convert IndexingProgress to a dictionary."""
        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["year"] = self.year
        if self.month is not None:
            data["month"] = self.month
        if self.last_message_id is not None:
            data["last_message_id"] = self.last_message_id
        data["total_messages"] = self.total_messages
        data["indexed_messages"] = self.indexed_messages
        if self.started_at is not None:
            data["started_at"] = self.started_at
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at
        data["completed"] = self.completed
        return data


@dataclass
class ClassificationLog:
    """Represents a classification audit log entry."""

    id: Optional[int] = None
    message_id: int = 0
    attempt_number: int = 1
    started_at: int = 0
    completed_at: Optional[int] = None
    total_duration_ms: Optional[int] = None
    final_category_id: Optional[int] = None
    final_confidence: Optional[float] = None
    final_method: Optional[str] = None
    call_chain: Optional[str] = None
    total_tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    error_occurred: bool = False
    error_message: Optional[str] = None
    error_step: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ClassificationLog":
        """Create a ClassificationLog from a dictionary."""
        return cls(
            id=data.get("id"),
            message_id=data.get("message_id", 0),
            attempt_number=data.get("attempt_number", 1),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at"),
            total_duration_ms=data.get("total_duration_ms"),
            final_category_id=data.get("final_category_id"),
            final_confidence=data.get("final_confidence"),
            final_method=data.get("final_method"),
            call_chain=data.get("call_chain"),
            total_tokens_used=data.get("total_tokens_used", 0),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            estimated_cost=data.get("estimated_cost", 0.0),
            error_occurred=bool(data.get("error_occurred", False)),
            error_message=data.get("error_message"),
            error_step=data.get("error_step"),
        )

    def to_dict(self) -> dict:
        """Convert ClassificationLog to a dictionary."""
        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["message_id"] = self.message_id
        data["attempt_number"] = self.attempt_number
        data["started_at"] = self.started_at
        if self.completed_at is not None:
            data["completed_at"] = self.completed_at
        if self.total_duration_ms is not None:
            data["total_duration_ms"] = self.total_duration_ms
        if self.final_category_id is not None:
            data["final_category_id"] = self.final_category_id
        if self.final_confidence is not None:
            data["final_confidence"] = self.final_confidence
        if self.final_method is not None:
            data["final_method"] = self.final_method
        if self.call_chain is not None:
            data["call_chain"] = self.call_chain
        data["total_tokens_used"] = self.total_tokens_used
        data["input_tokens"] = self.input_tokens
        data["output_tokens"] = self.output_tokens
        data["estimated_cost"] = self.estimated_cost
        data["error_occurred"] = self.error_occurred
        if self.error_message is not None:
            data["error_message"] = self.error_message
        if self.error_step is not None:
            data["error_step"] = self.error_step
        return data


@dataclass
class ClassificationStep:
    """Represents a single classification step."""

    id: Optional[int] = None
    log_id: int = 0
    step_number: int = 0
    step_type: str = ""
    started_at: int = 0
    completed_at: Optional[int] = None
    duration_ms: Optional[int] = None
    input_data_preview: Optional[str] = None
    input_tokens: int = 0
    predicted_category: Optional[str] = None
    confidence: Optional[float] = None
    output_tokens: int = 0
    raw_response: Optional[str] = None
    step_cost: float = 0.0
    model_name: Optional[str] = None
    was_accepted: bool = False
    acceptance_reason: Optional[str] = None
    matched_rules: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ClassificationStep":
        """Create a ClassificationStep from a dictionary."""
        import json

        matched_rules = data.get("matched_rules")
        if isinstance(matched_rules, str):
            matched_rules = json.loads(matched_rules) if matched_rules else None

        return cls(
            id=data.get("id"),
            log_id=data.get("log_id", 0),
            step_number=data.get("step_number", 0),
            step_type=data.get("step_type", ""),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at"),
            duration_ms=data.get("duration_ms"),
            input_data_preview=data.get("input_data_preview"),
            input_tokens=data.get("input_tokens", 0),
            predicted_category=data.get("predicted_category"),
            confidence=data.get("confidence"),
            output_tokens=data.get("output_tokens", 0),
            raw_response=data.get("raw_response"),
            step_cost=data.get("step_cost", 0.0),
            model_name=data.get("model_name"),
            was_accepted=bool(data.get("was_accepted", False)),
            acceptance_reason=data.get("acceptance_reason"),
            matched_rules=matched_rules,
        )

    def to_dict(self) -> dict:
        """Convert ClassificationStep to a dictionary."""
        import json

        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["log_id"] = self.log_id
        data["step_number"] = self.step_number
        data["step_type"] = self.step_type
        data["started_at"] = self.started_at
        if self.completed_at is not None:
            data["completed_at"] = self.completed_at
        if self.duration_ms is not None:
            data["duration_ms"] = self.duration_ms
        if self.input_data_preview is not None:
            data["input_data_preview"] = self.input_data_preview
        data["input_tokens"] = self.input_tokens
        if self.predicted_category is not None:
            data["predicted_category"] = self.predicted_category
        if self.confidence is not None:
            data["confidence"] = self.confidence
        data["output_tokens"] = self.output_tokens
        if self.raw_response is not None:
            data["raw_response"] = self.raw_response
        data["step_cost"] = self.step_cost
        if self.model_name is not None:
            data["model_name"] = self.model_name
        data["was_accepted"] = self.was_accepted
        if self.acceptance_reason is not None:
            data["acceptance_reason"] = self.acceptance_reason
        if self.matched_rules is not None:
            data["matched_rules"] = json.dumps(self.matched_rules)
        return data


@dataclass
class LearnedRule:
    """Represents a learned categorization rule."""

    id: Optional[int] = None
    category_id: int = 0
    rule_type: str = ""
    rule_value: str = ""
    occurrence_count: int = 1
    correct_count: int = 1
    accuracy: Optional[float] = None
    avg_confidence: Optional[float] = None
    first_seen: Optional[int] = None
    last_seen: Optional[int] = None
    promoted_to_production: bool = False
    promoted_at: Optional[int] = None
    human_reviewed: bool = False
    human_approved: bool = False
    reviewed_by: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "LearnedRule":
        """Create a LearnedRule from a dictionary."""
        return cls(
            id=data.get("id"),
            category_id=data.get("category_id", 0),
            rule_type=data.get("rule_type", ""),
            rule_value=data.get("rule_value", ""),
            occurrence_count=data.get("occurrence_count", 1),
            correct_count=data.get("correct_count", 1),
            accuracy=data.get("accuracy"),
            avg_confidence=data.get("avg_confidence"),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            promoted_to_production=bool(data.get("promoted_to_production", False)),
            promoted_at=data.get("promoted_at"),
            human_reviewed=bool(data.get("human_reviewed", False)),
            human_approved=bool(data.get("human_approved", False)),
            reviewed_by=data.get("reviewed_by"),
            notes=data.get("notes"),
        )

    def to_dict(self) -> dict:
        """Convert LearnedRule to a dictionary."""
        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["category_id"] = self.category_id
        data["rule_type"] = self.rule_type
        data["rule_value"] = self.rule_value
        data["occurrence_count"] = self.occurrence_count
        data["correct_count"] = self.correct_count
        if self.accuracy is not None:
            data["accuracy"] = self.accuracy
        if self.avg_confidence is not None:
            data["avg_confidence"] = self.avg_confidence
        if self.first_seen is not None:
            data["first_seen"] = self.first_seen
        if self.last_seen is not None:
            data["last_seen"] = self.last_seen
        data["promoted_to_production"] = self.promoted_to_production
        if self.promoted_at is not None:
            data["promoted_at"] = self.promoted_at
        data["human_reviewed"] = self.human_reviewed
        data["human_approved"] = self.human_approved
        if self.reviewed_by is not None:
            data["reviewed_by"] = self.reviewed_by
        if self.notes is not None:
            data["notes"] = self.notes
        return data


@dataclass
class ActionQueueItem:
    """Represents an item in the action queue."""

    id: Optional[int] = None
    message_id: int = 0
    action_type: str = ""
    action_data: Optional[dict] = None
    priority: int = 5
    status: str = "pending"
    scheduled_at: Optional[int] = None
    executed_at: Optional[int] = None
    error_message: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "ActionQueueItem":
        """Create an ActionQueueItem from a dictionary."""
        import json

        action_data = data.get("action_data")
        if isinstance(action_data, str):
            action_data = json.loads(action_data) if action_data else None

        return cls(
            id=data.get("id"),
            message_id=data.get("message_id", 0),
            action_type=data.get("action_type", ""),
            action_data=action_data,
            priority=data.get("priority", 5),
            status=data.get("status", "pending"),
            scheduled_at=data.get("scheduled_at"),
            executed_at=data.get("executed_at"),
            error_message=data.get("error_message"),
        )

    def to_dict(self) -> dict:
        """Convert ActionQueueItem to a dictionary."""
        import json

        data = {}
        if self.id is not None:
            data["id"] = self.id
        data["message_id"] = self.message_id
        data["action_type"] = self.action_type
        if self.action_data is not None:
            data["action_data"] = json.dumps(self.action_data)
        data["priority"] = self.priority
        data["status"] = self.status
        if self.scheduled_at is not None:
            data["scheduled_at"] = self.scheduled_at
        if self.executed_at is not None:
            data["executed_at"] = self.executed_at
        if self.error_message is not None:
            data["error_message"] = self.error_message
        return data
