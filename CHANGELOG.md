# Changelog

## [Unreleased]

### Added
- The request format v1 (`App`, `Database`) as Pydantic models, generated JSON Schema in `schema/`, and
  `shikigami-validate`, which checks a Meru/requests checkout: every file valid, names match file names, a database
  names an existing app (one per app), hosts unique, nothing but requests in the repo.
