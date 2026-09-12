import React from 'react';
import './DiscussionVisualization.css';

const DiscussionVisualization = ({ discussion, conclusion }) => {
  if (!discussion || !discussion.messages) {
    return <div className="no-discussion">暂无会诊记录</div>;
  }

  return (
    <div className="discussion-container">
      <div className="discussion-header">
        <h3>MDT会诊讨论</h3>
        <div className="main-issue">
          <strong>主要问题：</strong>{discussion.main_issue}
        </div>
      </div>

      <div className="messages-container">
        {discussion.messages.map((message, index) => (
          <div key={index} className="message-card">
            <div className="message-header">
              <span className="agent-name">{message.agent}</span>
              <span className="timestamp">{new Date(message.timestamp).toLocaleTimeString()}</span>
            </div>
            <div className="message-content">{message.content}</div>
          </div>
        ))}
      </div>

      {conclusion && (
        <div className="conclusion-section">
          <h4>最终结论</h4>
          <div className="conclusion-content">{conclusion}</div>
        </div>
      )}
    </div>
  );
};

export default DiscussionVisualization;
