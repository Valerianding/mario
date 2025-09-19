"""
PPO马里奥训练脚本

这个脚本实现了完整的PPO训练流程：
1. 创建并行马里奥环境
2. 初始化PPO算法
3. 数据收集和经验回放
4. 网络更新
5. 性能监控和模型保存

使用方法:
python train.py
"""

import os
import time
import argparse
import numpy as np
import torch
from tqdm import tqdm

# 导入我们的模块
from config import Config
from enviroments.parallel_envs import create_parallel_mario_envs
from algorithms.ppo import create_ppo_algorithm
from utils.replay_buffer import RolloutBuffer
from utils.logger import TrainingLogger, PerformanceMonitor, ProgressTracker
from algorithms.base import ModelManager

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='PPO Mario Training')
    
    # 环境参数
    parser.add_argument('--num_envs', type=int, default=Config.NUM_ENVS,
                       help='并行环境数量')
    # 关卡选择改由配置 Config.WORLD_STAGE 控制，取消命令行覆盖
    parser.add_argument('--render_env', type=int, default=None,
                       help='需要渲染的环境ID（用于观察训练过程）')
    
    # 训练参数
    parser.add_argument('--max_episodes', type=int, default=Config.MAX_EPISODES,
                       help='最大训练回合数')
    parser.add_argument('--max_steps', type=int, default=Config.MAX_STEPS,
                       help='最大训练步数')
    parser.add_argument('--save_freq', type=int, default=Config.SAVE_FREQ,
                       help='保存模型频率（按更新次数）')
    parser.add_argument('--log_freq', type=int, default=Config.LOG_FREQ,
                       help='日志记录频率（按更新次数）')
    
    # PPO参数
    parser.add_argument('--learning_rate', type=float, default=Config.LEARNING_RATE,
                       help='学习率')
    parser.add_argument('--ppo_epochs', type=int, default=Config.PPO_EPOCHS,
                       help='PPO更新轮数')
    parser.add_argument('--clip_epsilon', type=float, default=Config.CLIP_EPSILON,
                       help='PPO裁剪参数')
    parser.add_argument('--steps_per_update', type=int, default=Config.STEPS_PER_UPDATE,
                       help='每次更新收集的步数')
    
    # 系统参数
    parser.add_argument('--device', type=str, default=None,
                       help='计算设备 (cuda/cpu)')
    parser.add_argument('--seed', type=int, default=Config.SEED,
                       help='随机种子')
    parser.add_argument('--resume', type=str, default=None,
                       help='恢复训练的模型路径')
    parser.add_argument('--experiment_name', type=str, default=None,
                       help='实验名称')
    
    return parser.parse_args()


def set_seed(seed):
    """设置随机种子"""
    torch.manual_seed(seed)
    np.random.seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    
    # 设置确定性计算（可能影响性能）
    # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.benchmark = False


class PPOTrainer:
    """PPO训练器类"""
    
    def __init__(self, args):
        """
        初始化训练器
        
        Args:
            args: 命令行参数
        """
        self.args = args
        self.device = torch.device(args.device) if args.device else Config.DEVICE
        print(f"使用设备: {self.device}")
        
        # 设置随机种子
        set_seed(args.seed)
        
        # 更新配置（支持命令行参数覆盖）
        Config.LEARNING_RATE = args.learning_rate
        Config.PPO_EPOCHS = args.ppo_epochs
        Config.CLIP_EPSILON = args.clip_epsilon
        Config.STEPS_PER_UPDATE = args.steps_per_update
        
        # 打印配置信息
        Config.print_config()
        
        # 创建目录
        os.makedirs(Config.MODEL_DIR, exist_ok=True)
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        
        # 初始化组件
        self._init_environment()
        self._init_algorithm() 
        self._init_buffer()
        self._init_logging()
        self._init_monitoring()
        
        print("PPO训练器初始化完成！")
    
    def _init_environment(self):
        """初始化环境"""
        print("创建并行马里奥环境...")
        
        self.envs = create_parallel_mario_envs(
            num_envs=self.args.num_envs,
            worlds=Config.WORLD_STAGE,
            use_subprocess=True,  # 使用多进程以获得更好的性能
            render_env_id=self.args.render_env
        )
        
        self.observation_space = self.envs.observation_space
        self.action_space = self.envs.action_space
        
        print(f"环境创建完成: {len(self.envs)} 个并行环境")
    
    def _init_algorithm(self):
        """初始化PPO算法"""
        print("初始化PPO算法...")
        
        self.ppo = create_ppo_algorithm(
            observation_space=self.observation_space,
            action_space=self.action_space,
            device=self.device,
            logger=None  # 稍后设置
        )
        
        # 如果有恢复的模型，加载它
        if self.args.resume:
            print(f"从 {self.args.resume} 恢复训练...")
            model_manager = ModelManager()
            model_manager.load_model(self.ppo, self.args.resume)
    
    def _init_buffer(self):
        """初始化经验缓冲区"""
        print("初始化经验缓冲区...")
        
        self.rollout_buffer = RolloutBuffer(
            buffer_size=Config.STEPS_PER_UPDATE,
            num_envs=self.args.num_envs,
            obs_shape=self.observation_space.shape,
            action_dim=1,  # 离散动作
            device=self.device
        )
        
        print(f"缓冲区大小: {len(self.rollout_buffer):,} 个转移")
    
    def _init_logging(self):
        """初始化日志记录"""
        print("初始化日志系统...")
        
        self.logger = TrainingLogger(
            log_dir=Config.LOG_DIR,
            experiment_name=self.args.experiment_name
        )
        
        # 设置算法的日志记录器
        self.ppo.logger = self.logger
        
        # 进度跟踪器
        self.progress_tracker = ProgressTracker(
            target_reward=Config.TARGET_REWARD,
            patience=Config.PATIENCE  # 合没有改进就可以考虑停止
        )
    
    def _init_monitoring(self):
        """初始化性能监控"""
        self.performance_monitor = PerformanceMonitor()
        self.model_manager = ModelManager()
        
        # 训练统计
        self.episode_rewards = []
        self.episode_lengths = []
        self.best_avg_reward = float('-inf')
        self.episodes_since_best = 0

    def _compute_world_sampling_weights(self, eval_stats):
        """
        根据评估结果计算各关卡的采样权重（表现差的权重更高）
        
        策略：
        - 从 eval_stats 中读取每个关卡的平均奖励 eval_avg_reward_X_Y
        - 以 (max_reward - reward) 作为困难度分数，分数越高权重越大
        - 应用放大系数 alpha，并加上最小权重下限，最后归一化
        """
        worlds = Config.WORLD_STAGE
        if isinstance(worlds, str):
            worlds = [worlds]
        if not worlds or len(worlds) == 1:
            return None

        # 收集各关卡平均奖励
        avg_rewards = {}
        for w in worlds:
            tag = w.replace('-', '_')
            key = f'eval_avg_reward_{tag}'
            avg_rewards[w] = float(eval_stats.get(key, eval_stats.get('eval_avg_reward', 0.0)))

        # 计算困难度分数
        max_avg = max(avg_rewards.values()) if avg_rewards else 0.0
        eps = 1e-6
        alpha = getattr(Config, 'WORLD_SAMPLING_ALPHA', 1.0)
        base = getattr(Config, 'WORLD_SAMPLING_MIN_WEIGHT', 0.05)

        raw_weights = {}
        for w, r in avg_rewards.items():
            score = max_avg - r  # 表现越差分数越高
            weight = (score + eps) ** alpha + base
            raw_weights[w] = weight

        # 归一化
        total = sum(raw_weights.values())
        if total <= 0:
            return None
        weights = {w: (raw_weights[w] / total) for w in worlds}
        return weights
    
    def collect_rollouts(self):
        """
        收集一批训练数据
        
        Returns:
            dict: 收集统计信息
        """
        self.ppo.eval()  # 设置为评估模式（关闭dropout等）
        
        # 重置缓冲区
        self.rollout_buffer.reset()
        
        # 重置环境并获取初始状态
        observations = self.envs.reset()
        
        # 收集统计
        collect_stats = {
            'episodes_completed': 0,
            'total_reward': 0.0,
            'avg_episode_length': 0.0,
        }
        
        episode_rewards = []
        episode_lengths = []
        current_episode_rewards = np.zeros(self.args.num_envs)
        current_episode_lengths = np.zeros(self.args.num_envs)
        
        # 收集指定步数的数据
        for step in range(Config.STEPS_PER_UPDATE):
            # 选择动作
            with torch.no_grad():
                actions, extra_info = self.ppo.act(observations)
                values = extra_info['values']
                log_probs = extra_info['log_probs']
            
            # 执行动作
            next_observations, rewards, dones, infos = self.envs.step(actions)
            
            # 存储转移
            self.rollout_buffer.add(
                states=observations,
                actions=actions,
                rewards=rewards,
                values=values,
                log_probs=log_probs,
                dones=dones
            )
            
            # 更新统计
            current_episode_rewards += rewards.cpu().numpy()
            current_episode_lengths += 1
            
            # 处理回合结束
            for i, done in enumerate(dones):
                if done:
                    episode_reward = current_episode_rewards[i]
                    episode_length = current_episode_lengths[i]
                    
                    episode_rewards.append(episode_reward)
                    episode_lengths.append(episode_length)
                    
                    # 记录到日志
                    info = infos[i] if i < len(infos) else {}
                    self.logger.log_episode(episode_reward, episode_length, info)
                    
                    # 更新进度跟踪
                    progress_info = self.progress_tracker.update(episode_reward)
                    
                    # 重置计数器
                    current_episode_rewards[i] = 0
                    current_episode_lengths[i] = 0
                    
                    collect_stats['episodes_completed'] += 1
            
            # 更新观察
            observations = next_observations
        
        # 计算最后状态的价值（用于GAE计算）
        with torch.no_grad():
            next_values = self.ppo.compute_value(next_observations)
        
        # 计算优势和回报
        self.rollout_buffer.compute_advantages_and_returns(
            next_values=next_values,
            gamma=Config.GAMMA,
            gae_lambda=Config.GAE_LAMBDA
        )
        
        # 更新收集统计
        if episode_rewards:
            collect_stats['total_reward'] = sum(episode_rewards)
            collect_stats['avg_episode_length'] = np.mean(episode_lengths)
            
            # 更新全局统计
            self.episode_rewards.extend(episode_rewards)
            self.episode_lengths.extend(episode_lengths)
        
        return collect_stats
    
    def train_step(self):
        """
        执行一次完整的训练步骤
        
        Returns:
            dict: 训练统计信息
        """
        # 1. 收集数据
        collect_stats = self.collect_rollouts()
        
        # 2. 更新策略
        self.ppo.train()  # 设置为训练模式
        update_stats = self.ppo.update(self.rollout_buffer)
        
        # 3. 合并统计信息
        train_stats = {**collect_stats, **update_stats}
        
        # 4. 更新总步数
        self.ppo.total_steps += Config.STEPS_PER_UPDATE * self.args.num_envs
        
        return train_stats
    
    def evaluate_model(self, num_episodes=5):
        """
        评估当前模型性能
        
        - 对配置中的所有关卡逐一评估（每关卡 num_episodes 回合）
        - 汇总整体与逐关卡统计，整体结果用于早停与最优模型判定
        
        Args:
            num_episodes (int): 每个关卡的评估回合数
            
        Returns:
            dict: 评估结果（包含整体与逐关卡统计）
        """
        self.ppo.eval()
        from enviroments.mario_env import create_mario_environment

        # 评估关卡由配置控制（兼容字符串/列表）
        worlds = Config.WORLD_STAGE
        if isinstance(worlds, str):
            worlds = [worlds]

        print(f"评估模型性能（每关卡 {num_episodes} 回合）：{worlds}")

        # 整体汇总
        all_rewards = []
        all_lengths = []

        # 逐关卡统计
        per_world_stats = {}

        for world in worlds:
            eval_env = create_mario_environment(world=world, render_mode=None)
            world_rewards = []
            world_lengths = []

            for episode in range(num_episodes):
                obs = eval_env.reset()
                obs = torch.FloatTensor(obs).unsqueeze(0).to(self.device)

                episode_reward = 0.0
                episode_length = 0
                done = False

                while not done:
                    with torch.no_grad():
                        actions, _ = self.ppo.act(obs, deterministic=True)
                    obs, reward, done, info = eval_env.step(actions.item())
                    obs = torch.FloatTensor(obs).unsqueeze(0).to(self.device)
                    episode_reward += reward
                    episode_length += 1
                    if episode_length > 5000:
                        break

                world_rewards.append(episode_reward)
                world_lengths.append(episode_length)
                print(f"  [{world}] 回合 {episode+1}: 奖励={episode_reward:.2f}, 长度={episode_length}")

            eval_env.close()

            # 记录逐关卡统计
            per_world_stats[world] = {
                'avg_reward': float(np.mean(world_rewards)) if world_rewards else 0.0,
                'std_reward': float(np.std(world_rewards)) if world_rewards else 0.0,
                'max_reward': float(np.max(world_rewards)) if world_rewards else 0.0,
                'min_reward': float(np.min(world_rewards)) if world_rewards else 0.0,
                'avg_length': float(np.mean(world_lengths)) if world_lengths else 0.0,
            }

            all_rewards.extend(world_rewards)
            all_lengths.extend(world_lengths)

        # 汇总整体统计（用于选最优/早停）
        eval_stats = {
            'eval_avg_reward': float(np.mean(all_rewards)) if all_rewards else 0.0,
            'eval_std_reward': float(np.std(all_rewards)) if all_rewards else 0.0,
            'eval_max_reward': float(np.max(all_rewards)) if all_rewards else 0.0,
            'eval_min_reward': float(np.min(all_rewards)) if all_rewards else 0.0,
            'eval_avg_length': float(np.mean(all_lengths)) if all_lengths else 0.0,
        }

        # 逐关卡指标也写入，便于日志系统记录（避免TensorBoard标签中的连字符，替换为下划线）
        for world, stats in per_world_stats.items():
            tag = world.replace('-', '_')
            eval_stats[f'eval_avg_reward_{tag}'] = stats['avg_reward']
            eval_stats[f'eval_std_reward_{tag}'] = stats['std_reward']
            eval_stats[f'eval_max_reward_{tag}'] = stats['max_reward']
            eval_stats[f'eval_min_reward_{tag}'] = stats['min_reward']
            eval_stats[f'eval_avg_length_{tag}'] = stats['avg_length']

        print(f"评估完成: 总体平均奖励={eval_stats['eval_avg_reward']:.2f} ± {eval_stats['eval_std_reward']:.2f}")
        return eval_stats
    
    def should_stop_training(self):
        """
        判断是否应该停止训练
        
        Returns:
            tuple: (是否停止, 停止原因)
        """
        # 检查最大步数
        if self.ppo.total_steps >= self.args.max_steps:
            return True, f"达到最大步数 {self.args.max_steps:,}"
        
        # 检查最大回合数
        if self.ppo.total_episodes >= self.args.max_episodes:
            return True, f"达到最大回合数 {self.args.max_episodes:,}"
        
        # 检查目标奖励
        if len(self.episode_rewards) >= 100:
            recent_avg = np.mean(self.episode_rewards[-100:])
            if recent_avg >= Config.TARGET_REWARD:
                return True, f"达到目标奖励 {Config.TARGET_REWARD} (当前: {recent_avg:.2f})"
        
        # 检查早停条件
        progress_info = self.progress_tracker.update(
            self.episode_rewards[-1] if self.episode_rewards else 0
        )
        if progress_info['should_stop']:
            return True, f"早停：连续 {progress_info['episodes_without_improvement']} 回合无改进"
        
        return False, ""
    
    def train(self):
        """主训练循环"""
        print("\n开始PPO训练...")
        print("=" * 60)
        
        start_time = time.time()
        update_count = 0
        
        try:
            while True:
                update_start_time = time.time()
                
                # 执行训练步骤
                train_stats = self.train_step()
                update_count += 1
                
                # 记录系统信息
                if self.performance_monitor:
                    system_info = self.performance_monitor.get_system_info()
                    self.logger.log_system_info(**system_info)
                
                # 定期打印统计信息
                if update_count % self.args.log_freq == 0:
                    update_time = time.time() - update_start_time
                    total_time = time.time() - start_time
                    
                    print(f"\n更新 #{update_count}")
                    print(f"总步数: {self.ppo.total_steps:,}")
                    print(f"总回合: {self.ppo.total_episodes:,}")
                    print(f"更新用时: {update_time:.2f}s")
                    print(f"总用时: {total_time/3600:.2f}h")
                    
                    if train_stats.get('episodes_completed', 0) > 0:
                        print(f"完成回合: {train_stats['episodes_completed']}")
                        print(f"平均奖励: {train_stats['total_reward']/train_stats['episodes_completed']:.2f}")
                    
                    print(f"策略损失: {train_stats.get('policy_loss', 0):.4f}")
                    print(f"价值损失: {train_stats.get('value_loss', 0):.4f}")
                    print(f"熵: {train_stats.get('entropy', 0):.4f}")
                    print(f"裁剪比例: {train_stats.get('clip_fraction', 0):.3f}")
                    print(f"学习率: {train_stats.get('learning_rate', 0):.2e}")
                    
                    # 显示最近表现
                    if len(self.episode_rewards) >= 10:
                        recent_10 = np.mean(self.episode_rewards[-10:])
                        recent_100 = np.mean(self.episode_rewards[-100:]) if len(self.episode_rewards) >= 100 else np.mean(self.episode_rewards)
                        print(f"最近10回合平均奖励: {recent_10:.2f}")
                        print(f"最近100回合平均奖励: {recent_100:.2f}")
                        print(f"历史最佳奖励: {max(self.episode_rewards):.2f}")
                
                # 定期保存模型
                if update_count % self.args.save_freq == 0:
                    # 评估模型
                    eval_stats = self.evaluate_model(num_episodes=3)
                    
                    # 检查是否是最佳模型
                    current_avg = eval_stats['eval_avg_reward']
                    is_best = current_avg > self.best_avg_reward
                    
                    if is_best:
                        self.best_avg_reward = current_avg
                        self.episodes_since_best = 0
                        print(f"🎉 发现更好的模型! 平均奖励: {current_avg:.2f}")
                    else:
                        self.episodes_since_best += 1
                    
                    # 保存模型
                    model_filename = f"ppo_mario_update_{update_count}.pth"
                    self.model_manager.save_model(
                        self.ppo, 
                        filename=model_filename,
                        is_best=is_best
                    )
                    
                    # 动态调整：1) 单环境内按权重切换 2) 通过子环境数量分配
                    weights = self._compute_world_sampling_weights(eval_stats)
                    if weights:
                        try:
                            if getattr(Config, 'DYNAMIC_WORLD_SAMPLING', False) and not getattr(Config, 'USE_DYNAMIC_WORLD_COUNTS', False):
                                self.envs.set_world_weights(weights)
                                print(f"已根据评估结果更新关卡采样权重: {weights}")
                            if getattr(Config, 'USE_DYNAMIC_WORLD_COUNTS', False):
                                self.envs.set_world_allocation(weights)
                        except Exception as e:
                            print(f"更新关卡采样配置失败: {e}")
                    
                    # 记录评估结果
                    self.logger.log_training_step(**eval_stats)
                
                # 检查停止条件
                should_stop, stop_reason = self.should_stop_training()
                if should_stop:
                    print(f"\n训练停止: {stop_reason}")
                    break
                
                # 每100次更新显示详细统计
                if update_count % 100 == 0:
                    self.logger.print_training_stats()
                    
                    # 显示环境统计
                    env_stats = self.envs.get_statistics()
                    print("环境统计:")
                    for key, value in env_stats.items():
                        if isinstance(value, float):
                            print(f"  {key}: {value:.4f}")
                        else:
                            print(f"  {key}: {value}")
        
        except KeyboardInterrupt:
            print("\n收到中断信号，保存模型并退出...")
            
        except Exception as e:
            print(f"\n训练过程中出现错误: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            # 最终保存
            final_model_path = "ppo_mario_final.pth"
            self.model_manager.save_model(self.ppo, filename=final_model_path)
            
            # 最终评估
            final_eval = self.evaluate_model(num_episodes=10)
            print(f"\n最终评估结果:")
            for key, value in final_eval.items():
                print(f"  {key}: {value:.4f}")
            
            # 清理资源
            self.envs.close()
            self.logger.close()
            
            total_time = time.time() - start_time
            print(f"\n训练完成! 总用时: {total_time/3600:.2f} 小时")
            print(f"最终模型保存在: {final_model_path}")


def main():
    """主函数"""
    # 解析命令行参数
    args = parse_args()
    
    print("PPO马里奥训练")
    print("=" * 60)
    print(f"设备: {torch.device(args.device) if args.device else Config.DEVICE}")
    print(f"并行环境数: {args.num_envs}")
    print(f"训练关卡: {Config.WORLD_STAGE}")
    print(f"最大回合数: {args.max_episodes:,}")
    print(f"最大步数: {args.max_steps:,}")
    print("=" * 60)
    
    # 创建训练器
    trainer = PPOTrainer(args)
    
    # 开始训练
    trainer.train()


if __name__ == "__main__":
    main()
