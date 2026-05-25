// Fallback static pricing card. Copy over PricingCard.jsx if the live agent stalls on stage.
const tiers = [
  {
    name: 'Starter',
    price: 9,
    features: ['1 project', '5 GB storage', 'Email support', 'Community access']
  },
  {
    name: 'Pro',
    price: 29,
    features: ['10 projects', '100 GB storage', 'Priority support', 'Advanced analytics']
  },
  {
    name: 'Team',
    price: 99,
    features: ['Unlimited projects', '1 TB storage', '24/7 support', 'SSO and audit logs']
  }
]

const styles = {
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3, minmax(220px, 1fr))',
    gap: '1.25rem',
    maxWidth: '960px',
    width: '100%'
  },
  card: {
    background: '#1e293b',
    border: '1px solid #334155',
    borderRadius: '12px',
    padding: '1.75rem',
    display: 'flex',
    flexDirection: 'column',
    gap: '1rem',
    color: '#e2e8f0'
  },
  name: { fontSize: '1.125rem', fontWeight: 600, margin: 0 },
  price: { fontSize: '2.25rem', fontWeight: 700, margin: 0 },
  per: { fontSize: '0.875rem', color: '#94a3b8' },
  list: { listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: '0.5rem', flex: 1 },
  item: { fontSize: '0.9rem', color: '#cbd5e1' },
  button: {
    marginTop: '0.5rem',
    padding: '0.65rem 1rem',
    background: '#6366f1',
    color: '#fff',
    border: 'none',
    borderRadius: '8px',
    fontWeight: 600,
    cursor: 'pointer'
  }
}

export default function PricingCard() {
  return (
    <div style={styles.grid}>
      {tiers.map((t) => (
        <div key={t.name} style={styles.card}>
          <h3 style={styles.name}>{t.name}</h3>
          <p style={styles.price}>
            ${t.price}
            <span style={styles.per}> /mo</span>
          </p>
          <ul style={styles.list}>
            {t.features.map((f) => (
              <li key={f} style={styles.item}>{f}</li>
            ))}
          </ul>
          <button style={styles.button} type="button">Choose plan</button>
        </div>
      ))}
    </div>
  )
}
