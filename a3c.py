# coding:utf-8

import os
import gym
import time
import random
import numpy as np
import tensorflow as tf
from threading import Thread
from skimage.color import rgb2gray
from skimage.transform import resize
from keras.models import Model
from keras.layers import Convolution2D, Flatten, Dense, Input

ENV_NAME = 'Breakout-v0'  # Environment name
FRAME_WIDTH = 84  # Resized frame width
FRAME_HEIGHT = 84  # Resized frame height
STATE_LENGTH = 4  # Number of most recent frames to produce the input to the network
INITIAL_LEARNING_RATE = 0.0007  # Initial learning rate used by RMSProp
DECAY = 0.99  # Decay factor used by RMSProp
MIN_GRAD = 0.1  # Constant added to the squared gradient in the denominator of the RMSProp update
NO_OP_STEPS = 30  # Maximum number of "do nothing" actions to be performed by the agent at the start of an episode
ACTION_INTERVAL = 4  # The agent sees only every 4th input
GAMMA = 0.99  # Discount factor
ENTROPY_BETA = 0.01  # Entropy weight
NUM_THREADS = 2  # Number of thread
GLOBAL_T_MAX = 5000000  # Number of time steps we train
THREAD_T_MAX = 5  # The frequency with which the policy and the value function are updated
SAVE_INTERVAL = 500000  # The frequency with which the network is saved
TRAIN = True
LOAD_NETWORK = False
SAVE_NETWORK_PATH = 'saved_networks/' + ENV_NAME
SAVE_SUMMARY_PATH = 'summary/' + ENV_NAME


class Agent():
    def __init__(self, num_actions):
        self.num_actions = num_actions
        self.repeated_action = 0

        # Create policy and value networks
        self.s, self.action_probs, self.state_value = self.build_networks()

        # Define loss and gradient update operation
        self.a, self.r, self.lr, self.loss, self.grad_update = self.build_training_op()

        self.sess = tf.InteractiveSession()
        self.saver = tf.train.Saver()
        self.summary_placeholders, self.update_ops, self.summary_op = self.setup_summary()
        self.summary_writer = tf.train.SummaryWriter(SAVE_SUMMARY_PATH, self.sess.graph)

        if not os.path.exists(SAVE_NETWORK_PATH):
            os.makedirs(SAVE_NETWORK_PATH)

        self.sess.run(tf.initialize_all_variables())

        # Load network
        if LOAD_NETWORK:
            self.load_network()

    def build_networks(self):
        s_in = Input(shape=(STATE_LENGTH, FRAME_WIDTH, FRAME_HEIGHT))
        shared = Convolution2D(16, 8, 8, subsample=(4, 4), activation='relu')(s_in)
        shared = Convolution2D(32, 4, 4, subsample=(2, 2), activation='relu')(shared)
        shared = Flatten()(shared)
        shared = Dense(256, activation='relu')(shared)
        p_out = Dense(self.num_actions, activation='softmax')(shared)
        v_out = Dense(1)(shared)

        policy_network = Model(input=s_in, output=p_out)
        value_network = Model(input=s_in, output=v_out)

        s = tf.placeholder(tf.float32, [None, STATE_LENGTH, FRAME_WIDTH, FRAME_HEIGHT])
        action_probs = policy_network(s)
        state_value = value_network(s)

        return s, action_probs, state_value

    def build_training_op(self):
        a = tf.placeholder(tf.int64, [None])
        r = tf.placeholder(tf.float32, [None])
        lr = tf.placeholder(tf.float32)

        # Convert action to one hot vector
        a_one_hot = tf.one_hot(a, self.num_actions, 1.0, 0.0)
        log_prob = tf.log(tf.reduce_sum(tf.mul(self.action_probs, a_one_hot), reduction_indices=1))
        entropy = -tf.reduce_sum(self.action_probs * tf.log(self.action_probs), reduction_indices=1)

        advantage = r - self.state_value

        p_loss = -(log_prob * advantage + ENTROPY_BETA * entropy)
        v_loss = tf.square(advantage)
        loss = tf.reduce_mean(p_loss + 0.5 * v_loss)

        optimizer = tf.train.RMSPropOptimizer(lr, decay=DECAY, epsilon=MIN_GRAD)
        grad_update = optimizer.minimize(loss)

        return a, r, lr, loss, grad_update

    def get_initial_state(self, observation, last_observation):
        processed_observation = np.maximum(observation, last_observation)
        processed_observation = resize(rgb2gray(processed_observation), (FRAME_WIDTH, FRAME_HEIGHT))
        state = [processed_observation for _ in range(STATE_LENGTH)]
        return np.stack(state, axis=0)

    def get_action(self, state, t):
        action = self.repeated_action

        if t % ACTION_INTERVAL == 0:
            probs = self.sess.run(self.action_probs, feed_dict={self.s: [state]})[0]

            # Subtract a tiny value from probabilities in order to avoid 'ValueError: sum(pvals[:-1]) > 1.0' in np.random.multinomial
            probs = probs - np.finfo(np.float32).epsneg

            action = np.nonzero(np.random.multinomial(1, probs))[0][0]

            self.repeated_action = action

        return action

    def run(self, state, terminal, t, t_start, state_batch, action_batch, reward_batch, learning_rate):
        if terminal:
            r = 0
        else:
            r = self.sess.run(self.state_value, feed_dict={self.s: [state]})[0]

        r_batch = np.zeros(t - t_start)

        for i in reversed(range(t_start, t)):
            r = reward_batch[i - t_start] + GAMMA * r
            r_batch[i - t_start] = r

        loss, _ = self.sess.run([self.loss, self.grad_update], feed_dict={
            self.s: state_batch,
            self.a: action_batch,
            self.r: r_batch,
            self.lr: learning_rate
        })

        return loss

    def save_network(self, global_t):
        save_path = self.saver.save(self.sess, SAVE_NETWORK_PATH + '/' + ENV_NAME, global_step=global_t)
        print('Successfully saved: ' + save_path)

    def load_network(self):
        checkpoint = tf.train.get_checkpoint_state(SAVE_NETWORK_PATH)
        if checkpoint and checkpoint.model_checkpoint_path:
            self.saver.restore(self.sess, checkpoint.model_checkpoint_path)
            print('Successfully loaded: ' + checkpoint.model_checkpoint_path)
        else:
            print('Training new network...')

    def write_summary(self, total_reward, duration, episode, total_loss):
        stats = [total_reward, duration, sum(total_loss) / len(total_loss)]
        for i in range(len(stats)):
            self.sess.run(self.update_ops[i], feed_dict={
                self.summary_placeholders[i]: float(stats[i])
            })
        summary_str = self.sess.run(self.summary_op)
        self.summary_writer.add_summary(summary_str, global_episode + 1)

    def setup_summary(self):
        episode_total_reward = tf.Variable(0.)
        tf.scalar_summary(ENV_NAME + '/Total Reward/Episode', episode_total_reward)
        episode_duration = tf.Variable(0.)
        tf.scalar_summary(ENV_NAME + '/Duration/Episode', episode_duration)
        episode_avg_loss = tf.Variable(0.)
        tf.scalar_summary(ENV_NAME + '/Average Loss/Episode', episode_avg_loss)
        summary_vars = [episode_total_reward, episode_duration, episode_avg_loss]
        summary_placeholders = [tf.placeholder(tf.float32) for _ in range(len(summary_vars))]
        update_ops = [summary_vars[i].assign(summary_placeholders[i]) for i in range(len(summary_vars))]
        summary_op = tf.merge_all_summaries()
        return summary_placeholders, update_ops, summary_op


def actor_learner_thread(thread_id, env, agent):
    global global_t, learning_rate, global_episode
    global_t = 0
    t = 0
    learning_rate = INITIAL_LEARNING_RATE
    lr_step = INITIAL_LEARNING_RATE / GLOBAL_T_MAX

    total_reward = 0
    total_loss = []
    duration = 0
    global_episode = 0
    episode = 0

    # Delay
    time.sleep(3 * thread_id)

    terminal = False
    observation = env.reset()
    for _ in range(random.randint(1, NO_OP_STEPS)):
        last_observation = observation
        observation, _, _, _ = env.step(0)  # Do nothing
    state = agent.get_initial_state(observation, last_observation)

    while global_t < GLOBAL_T_MAX:
        t_start = t

        state_batch = []
        action_batch = []
        reward_batch = []

        while not (terminal or ((t - t_start) == THREAD_T_MAX)):
            last_observation = observation

            action = agent.get_action(state, t)

            observation, reward, terminal, _ = env.step(action)

            state_batch.append(state)
            action_batch.append(action)
            reward = np.clip(reward, -1, 1)
            reward_batch.append(reward)

            processed_observation = preprocess(observation, last_observation)
            next_state = np.append(state[1:, :, :], processed_observation, axis=0)

            t += 1
            global_t += 1

            total_reward += reward
            duration += 1

            # Anneal learning rate linearly over time
            learning_rate -= lr_step
            if learning_rate < 0.0:
                learning_rate = 0.0

            state = next_state

        loss = agent.run(state, terminal, t, t_start, state_batch, action_batch, reward_batch, learning_rate)
        total_loss.append(loss)

        # Save network
        if global_t % SAVE_INTERVAL == 0:
            agent.save_network(global_t)

        if terminal:
            # Write summary
            agent.write_summary(total_reward, duration, global_episode, total_loss)

            # Debug
            print('THREAD: {0:2d} / EPISODE: {1:4d} / GLOBAL_EPISODE: {2:6d} / LOCAL_TIME: {3:8d} / DURATION: {4:5d} / GLOBAL_TIME: {5:10d} / TOTAL_REWARD: {6:3.0f} / AVG_LOSS: {7:.5f}'.format(
                thread_id + 1, episode + 1, global_episode + 1, t, duration, global_t, total_reward, sum(total_loss) / len(total_loss)))

            total_reward = 0
            total_loss = []
            duration = 0
            episode += 1
            global_episode += 1

            terminal = False
            observation = env.reset()
            for _ in range(random.randint(1, NO_OP_STEPS)):
                last_observation = observation
                observation, _, _, _ = env.step(0)  # Do nothing
            state = agent.get_initial_state(observation, last_observation)


def preprocess(observation, last_observation):
    processed_observation = np.maximum(observation, last_observation)
    processed_observation = resize(rgb2gray(processed_observation), (FRAME_WIDTH, FRAME_HEIGHT))
    return np.reshape(processed_observation, (1, FRAME_WIDTH, FRAME_HEIGHT))


def main():
    envs = [gym.make(ENV_NAME) for _ in range(NUM_THREADS)]
    agent = Agent(num_actions=envs[0].action_space.n)

    if TRAIN:
        actor_learner_threads = [Thread(target=actor_learner_thread, args=(i, envs[i], agent)) for i in range(NUM_THREADS)]

        for thread in actor_learner_threads:
            thread.start()

        while True:
            for env in envs:
                env.render()

        for thread in actor_learner_threads:
            thread.join()


if __name__ == '__main__':
    main()
