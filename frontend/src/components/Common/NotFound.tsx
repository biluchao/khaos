import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Result } from 'antd';

const NotFound: React.FC = () => {
  const navigate = useNavigate();

  return (
    <Result
      status="404"
      title="404"
      subTitle="抱歉，您访问的页面不存在。"
      extra={
        <Button type="primary" onClick={() => navigate('/dashboard', { replace: true })}>
          返回仪表盘
        </Button>
      }
    />
  );
};

export default NotFound;
