import React, { useState, useEffect } from 'react';
import PatientForm from './components/PatientForm';
import DiscussionVisualization from './components/DiscussionVisualization';
import './App.css';

function App() {
  const [discussion, setDiscussion] = useState(null);
  const [conclusion, setConclusion] = useState('');
  const [isConsulting, setIsConsulting] = useState(false);

  const handleFormSubmit = async (formData) => {
    setIsConsulting(true);
    
    try {
      // 启动会诊
      const response = await fetch('http://localhost:5000/api/start_consultation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData)
      });
      
      if (response.ok) {
        // 开始讨论
        setTimeout(async () => {
          const discussionResponse = await fetch('http://localhost:5000/api/conduct_discussion', {
            method: 'POST'
          });
          
          const result = await discussionResponse.json();
          setDiscussion(result);
          setConclusion(result.conclusion);
          setIsConsulting(false);
        }, 2000);
      }
    } catch (error) {
      console.error('Error:', error);
      setIsConsulting(false);
    }
  };

  return (
    <div className="App">
      <header className="app-header">
        <h1>智能MDT多学科会诊系统</h1>
      </header>
      
      <main className="app-main">
        <PatientForm onSubmit={handleFormSubmit} isConsulting={isConsulting} />
        <DiscussionVisualization discussion={discussion} conclusion={conclusion} />
      </main>
    </div>
  );
}

export default App;