import React from 'react'
import { Routes, Route } from 'react-router-dom'
import Home from './pages/Home.jsx'

export default function App() {
  return (
    <main className="app">
      <h1>SixCoder Electron</h1>
      <Routes>
        <Route path="/" element={<Home />} />
      </Routes>
    </main>
  )
}
