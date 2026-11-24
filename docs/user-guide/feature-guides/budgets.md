# Budgets

Per-agent spending caps in the `budget` block: `daily_usd`, `weekly_usd`,
`monthly_usd`. `alert_at` (default 0.8) warns at 80% of the cap.

What happens at the cap is your choice: set `fallback_model` to switch to
something cheaper and keep working, or `hard_stop: true` to stop the agent.
With neither, you get the alert and the agent continues.

Budgets emit a `budget.exceeded` event, so you can route it to an outbound
webhook.
