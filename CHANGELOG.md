# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial release of graftpunk as a standalone package
- Encrypted session persistence with Fernet (AES-128-CBC + HMAC-SHA256)
- Stealth browser automation with undetected-chromedriver and selenium-stealth
- Pluggable storage backends: local filesystem, Supabase, S3
- Session keepalive daemon with customizable handlers
- Plugin architecture via Python entry points
- MFA support: TOTP generation, reCAPTCHA detection, magic link extraction
- CLI interface for session management
- Full type annotations with py.typed marker

### Storage Backends

- **Local**: File-based storage with configurable directory
- **Supabase**: Cloud storage with Vault integration for key management
- **S3**: AWS S3 bucket storage

### Security

- Fernet encryption for all session data
- SHA-256 checksum validation before deserialization
- 0600 permissions on local key files
- Supabase Vault integration for cloud key storage
