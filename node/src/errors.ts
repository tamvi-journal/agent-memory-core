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
