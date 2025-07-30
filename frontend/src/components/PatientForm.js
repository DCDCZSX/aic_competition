import React, { useState } from 'react';
import './PatientForm.css';

const PatientForm = ({ onSubmit, isConsulting }) => {
  const [formData, setFormData] = useState({
    name: '',
    age: '',
    gender: '',
    medical_history: '',
    main_issue: ''  // 新增主要问题字段
  });

  const handleSubmit = (e) => {
    e.preventDefault();
    onSubmit(formData);
  };

  return (
    <div className="patient-form">
      <h2>患者信息录入</h2>
      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label>患者姓名:</label>
          <input
            type="text"
            value={formData.name}
            onChange={(e) => setFormData({...formData, name: e.target.value})}
            required
          />
        </div>
        
        <div className="form-row">
          <div className="form-group">
            <label>年龄:</label>
            <input
              type="number"
              value={formData.age}
              onChange={(e) => setFormData({...formData, age: e.target.value})}
              required
            />
          </div>
          <div className="form-group">
            <label>性别:</label>
            <select
              value={formData.gender}
              onChange={(e) => setFormData({...formData, gender: e.target.value})}
              required
            >
              <option value="">请选择</option>
              <option value="男">男</option>
              <option value="女">女</option>
            </select>
          </div>
        </div>

        <div className="form-group">
          <label>病历信息:</label>
          <textarea
            value={formData.medical_history}
            onChange={(e) => setFormData({...formData, medical_history: e.target.value})}
            rows="4"
            placeholder="请输入患者的详细病历信息..."
            required
          />
        </div>

        <div className="form-group">
          <label>主要会诊问题:</label>
          <textarea
            value={formData.main_issue}
            onChange={(e) => setFormData({...formData, main_issue: e.target.value})}
            rows="3"
            placeholder="请输入本次MDT会诊需要重点讨论的问题..."
            required
          />
        </div>

        <button 
          type="submit" 
          className={`submit-btn ${isConsulting ? 'consulting' : ''}`}
          disabled={isConsulting}
        >
          {isConsulting ? '正在会诊中...' : '启动会诊'}
        </button>
      </form>
    </div>
  );
};

export default PatientForm;
