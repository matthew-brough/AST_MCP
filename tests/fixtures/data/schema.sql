-- Users of the system.
CREATE TABLE users (
  id INT PRIMARY KEY,
  email TEXT
);

CREATE VIEW active_users AS SELECT 1;

CREATE INDEX users_email_idx ON users (email);
