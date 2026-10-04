-- Roles and database for the test suite (local throwaway PostgreSQL or the CI service container).
-- Run as a superuser against a server that trusts test connections, so no password appears here.
-- Neither role is a superuser or bypasses row-level security; the app role inherits nothing.
CREATE ROLE fabrication_prep_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE;
CREATE ROLE fabrication_prep_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOINHERIT;
CREATE DATABASE fabrication_prep_test OWNER fabrication_prep_owner;
