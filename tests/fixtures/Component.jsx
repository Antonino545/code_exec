import React, { useState } from 'react';

/**
 * Edge-case JSX component testing quotes, self-closing formatting,
 * nested curly braces, and lone '>' lines.
 */
export function DashboardCard({ title, count, onRefresh }) {
  const [isActive, setIsActive] = useState(false);

  return (
    <section
      className="card-container border rounded shadow-md"
      data-active={isActive}
    >
      <header className="flex justify-between items-center">
        <h2 className='card-title text-xl font-bold'>{title}</h2>
        <span className="badge badge-primary">{count}</span>
      </header>

      {/* Test self-closing variation and lone '>' */}
      <img
        src="/assets/icons/analytics.svg"
        alt="Analytics Overview"
        className="w-12 h-12"
      />

      <div className="card-actions mt-4">
        <button
          type="button"
          onClick={() => setIsActive(!isActive)}
          className={`btn ${isActive ? 'btn-danger' : 'btn-outline'}`}
        >
          Toggle Status
        </button>

        <button
          type="button"
          onClick={onRefresh}
          disabled={count === 0}
        >
          Refresh Data
        </button>
      </div>
    </section>
  );
}

export default DashboardCard;
