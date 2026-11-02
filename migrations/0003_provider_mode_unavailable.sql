-- Record attempted calls while the live provider is unavailable (no key), so the failure is visible in evidence.
ALTER TABLE provider_calls DROP CONSTRAINT provider_calls_mode_check;
ALTER TABLE provider_calls ADD CONSTRAINT provider_calls_mode_check CHECK (mode IN ('live', 'mock', 'b0', 'unavailable'));
