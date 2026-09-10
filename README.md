# Veyra Phase 6 — Analytics + Final UX

Final roadmap phase for Veyra.

Included:
- 14-day sales/profit/order analytics in Admin
- Average order value and net profit margin KPIs
- Top products by recorded sales
- Analytics responsive/mobile layout
- EN/DE/FR/SQ analytics and UX translations
- Language selector click/touch fix with Escape/outside-click handling
- Theme selector click/touch fix and localStorage persistence
- Product cards remain fully clickable, with wishlist button isolated
- Navigation search now preserves the query and opens the Products section
- CSS cache-busting version so browser caches do not hide visual changes
- Responsive final polish for analytics and controls

Before production:
- Set a strong SECRET_KEY
- Configure PostgreSQL on Railway
- Configure Telegram and Stripe secrets as Railway variables
- Use a Telegram webhook/dedicated worker instead of multi-worker polling
- Add proper DB migrations (Alembic/Flask-Migrate) for future schema changes
- Only sell digital products/subscriptions you are authorized to resell and follow provider terms and EU/German requirements
