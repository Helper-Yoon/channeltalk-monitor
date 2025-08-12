import fetch from 'node-fetch';

export class ChannelTalkService {
  constructor(redisClient, io) {
    this.redis = redisClient;
    this.io = io;
    this.apiKey = process.env.CHANNEL_API_KEY || '688a26176fcb19aebf8b';
    this.apiSecret = process.env.CHANNEL_API_SECRET || 'a0db6c38b95c8ec4d9bb46e7c653b3e2';
    this.baseURL = 'https://api.channel.io/open/v5';
  }

  async makeRequest(endpoint, options = {}) {
    const url = `${this.baseURL}${endpoint}`;
    const headers = {
      'x-access-key': this.apiKey,
      'x-access-secret': this.apiSecret,
      'accept': 'application/json',
      ...options.headers
    };

    try {
      const response = await fetch(url, { 
        ...options, 
        headers,
        timeout: 10000 
      });
      
      if (!response.ok) {
        const errorText = await response.text();
        console.error(`API Error: ${response.status} - ${errorText}`);
        throw new Error(`HTTP ${response.status}`);
      }
      
      return await response.json();
    } catch (error) {
      console.error('Channel API request failed:', error);
      throw error;
    }
  }

  async syncOpenChats() {
    try {
      console.log('Syncing open chats...');
      const data = await this.makeRequest('/user-chats?state=opened&limit=200');
      const userChats = data.userChats || [];
      
      // 미답변 상담 필터링
      const unansweredChats = userChats.filter(chat => {
        if (chat.state !== 'opened') return false;
        
        // 고객이 마지막으로 메시지를 보낸 경우
        return chat.frontUpdatedAt > (chat.deskUpdatedAt || 0);
      });
      
      console.log(`Found ${unansweredChats.length} unanswered consultations`);
      
      // Redis에 저장
      for (const chat of unansweredChats) {
        const consultationData = {
          id: chat.id,
          customerName: chat.user?.name || '익명',
          customerMessage: chat.message?.plainText || '',
          team: this.assignTeam(chat.assignee),
          counselor: chat.assignee?.name || null,
          waitTime: this.calculateWaitTime(chat.frontUpdatedAt),
          createdAt: chat.createdAt,
          frontUpdatedAt: chat.frontUpdatedAt,
          chatUrl: `https://desk.channel.io/#/channels/197228/user_chats/${chat.id}`
        };
        
        // Redis에 저장
        await this.redis.hSet(
          `consultation:${chat.id}`, 
          Object.entries(consultationData).map(([k, v]) => [k, String(v)]).flat()
        );
        
        await this.redis.zAdd('consultations:waiting', {
          score: chat.frontUpdatedAt,
          value: String(chat.id)
        });
        
        // TTL 설정 (24시간)
        await this.redis.expire(`consultation:${chat.id}`, 86400);
      }
      
      // 대시보드 업데이트
      const currentData = await this.getUnansweredConsultations();
      this.io.to('dashboard').emit('dashboard:update', currentData);
      
    } catch (error) {
      console.error('Sync error:', error);
    }
  }

  assignTeam(assignee) {
    if (!assignee) return '미배정';
    const name = assignee.name || '';
    
    if (name.includes('SNS 1팀')) return 'SNS 1팀';
    if (name.includes('SNS 2팀')) return 'SNS 2팀';
    if (name.includes('SNS 3팀')) return 'SNS 3팀';
    if (name.includes('SNS 4팀')) return 'SNS 4팀';
    if (name.includes('의정부')) return '의정부 SNS팀';
    
    return 'SNS 1팀';
  }

  calculateWaitTime(frontUpdatedAt) {
    const now = Date.now();
    const waitTimeMs = now - frontUpdatedAt;
    return Math.floor(waitTimeMs / 1000 / 60);
  }

  async getUnansweredConsultations() {
    try {
      const consultationIds = await this.redis.zRange('consultations:waiting', 0, -1);
      const consultations = [];
      
      for (const id of consultationIds) {
        const data = await this.redis.hGetAll(`consultation:${id}`);
        if (data && Object.keys(data).length > 0) {
          data.waitTime = this.calculateWaitTime(parseInt(data.frontUpdatedAt));
          consultations.push(data);
        }
      }
      
      return consultations.sort((a, b) => b.waitTime - a.waitTime);
    } catch (error) {
      console.error('Error getting consultations:', error);
      return [];
    }
  }
}
