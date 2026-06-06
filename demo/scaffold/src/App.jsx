import PricingCard from './PricingCard.jsx'

const wrapperStyle = {
  minHeight: '100vh',
  width: '100%',
  background: '#0f172a',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '2rem',
  fontFamily: 'system-ui, -apple-system, Segoe UI, sans-serif',
  color: '#e2e8f0'
}

export default function App() {
  return (
    <div style={wrapperStyle}>
      <PricingCard />
    </div>
  )
}
