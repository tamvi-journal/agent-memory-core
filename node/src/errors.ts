export class SchemaVersionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SchemaVersionError";
  }
}

export class MigrationRequired extends SchemaVersionError {
  constructor(message: string) {
    super(message);
    this.name = "MigrationRequired";
  }
}

export class IncompatibleJournalMode extends SchemaVersionError {
  constructor(message: string) {
    super(message);
    this.name = "IncompatibleJournalMode";
  }
}

export class AuthorityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}
export class ReceiptNotFound extends AuthorityError {}
export class ReceiptIntegrityError extends AuthorityError {}
export class ProposalIntegrityError extends AuthorityError {}
export class ProposalDecided extends AuthorityError {}
export class StaleAuthority extends AuthorityError {}
export class HumanPresenceRequired extends AuthorityError {}
export class ConfirmationMismatch extends AuthorityError {}
export class PinnedRecordError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PinnedRecordError";
  }
}

export class ValueError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValueError";
  }
}

export class RuntimeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RuntimeError";
  }
}

export class FileExistsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FileExistsError";
  }
}
